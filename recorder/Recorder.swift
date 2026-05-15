// Recorder — Swift CLI that captures system audio (via ScreenCaptureKit)
// and microphone audio (via AVAudioEngine) to two WAV files in <output_dir>.
//
// Usage:  ./recorder <output_dir>
//
// Output: <output_dir>/system.wav  (16 kHz mono PCM, the other side of the call)
//         <output_dir>/mic.wav     (16 kHz mono PCM, the user's voice)
//
// Stops cleanly on SIGINT (Ctrl-C from a terminal, or `kill -INT <pid>` from
// the parent Python process). Prints one JSON event per line on stdout so the
// orchestrator can poll state.

import Foundation
import ScreenCaptureKit
import AVFoundation

// MARK: - JSON event emitter -------------------------------------------------

/// Atomic stdout writer so events from different queues don't interleave.
let stdoutLock = NSLock()

func emit(_ event: String, _ extras: [String: Any] = [:]) {
    var dict: [String: Any] = [
        "event": event,
        "ts": Date().timeIntervalSince1970,
    ]
    for (k, v) in extras { dict[k] = v }
    guard let data = try? JSONSerialization.data(withJSONObject: dict, options: []),
          let s = String(data: data, encoding: .utf8) else { return }
    stdoutLock.lock()
    print(s)
    fflush(stdout)
    stdoutLock.unlock()
}

func emitError(_ message: String, code: Int = -1) {
    emit("error", ["message": message, "code": code])
}

// MARK: - WAV audio settings -------------------------------------------------

let targetSampleRate: Double = 16_000
let targetChannelCount: AVAudioChannelCount = 1

/// Settings dict for AVAudioFile when writing a 16 kHz mono PCM WAV.
let wavSettings: [String: Any] = [
    AVFormatIDKey: kAudioFormatLinearPCM,
    AVSampleRateKey: targetSampleRate,
    AVNumberOfChannelsKey: targetChannelCount,
    AVLinearPCMBitDepthKey: 16,
    AVLinearPCMIsFloatKey: false,
    AVLinearPCMIsBigEndianKey: false,
    AVLinearPCMIsNonInterleaved: false,
]

// MARK: - Microphone recorder -----------------------------------------------

final class MicRecorder {
    private let engine = AVAudioEngine()
    private var audioFile: AVAudioFile?
    private var converter: AVAudioConverter?
    private let outputURL: URL
    private var samplesWritten: Int64 = 0

    init(outputURL: URL) {
        self.outputURL = outputURL
    }

    func start() throws {
        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)

        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0 else {
            throw NSError(domain: "MicRecorder", code: 1,
                          userInfo: [NSLocalizedDescriptionKey:
                            "input format invalid (no mic permission?)"])
        }

        // Target: 16 kHz mono PCM int16.
        guard let targetFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: targetSampleRate,
            channels: targetChannelCount,
            interleaved: true
        ) else {
            throw NSError(domain: "MicRecorder", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "target format invalid"])
        }

        audioFile = try AVAudioFile(forWriting: outputURL, settings: wavSettings)
        converter = AVAudioConverter(from: inputFormat, to: targetFormat)

        input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { [weak self] buf, _ in
            guard let self = self,
                  let converter = self.converter,
                  let outFile = self.audioFile else { return }

            // Calculate output capacity based on sample rate ratio.
            let ratio = targetSampleRate / inputFormat.sampleRate
            let outCapacity = AVAudioFrameCount(Double(buf.frameLength) * ratio + 1024)
            guard let outBuf = AVAudioPCMBuffer(pcmFormat: targetFormat,
                                                 frameCapacity: outCapacity) else { return }

            var error: NSError?
            let status = converter.convert(to: outBuf, error: &error) { _, statusPtr in
                statusPtr.pointee = .haveData
                return buf
            }
            if status == .error || error != nil {
                emitError("mic convert failed: \(error?.localizedDescription ?? "unknown")")
                return
            }
            do {
                try outFile.write(from: outBuf)
                self.samplesWritten += Int64(outBuf.frameLength)
            } catch {
                emitError("mic write failed: \(error.localizedDescription)")
            }
        }

        try engine.start()
        emit("mic_started", [
            "path": outputURL.path,
            "input_sr": inputFormat.sampleRate,
            "input_ch": inputFormat.channelCount,
        ])
    }

    func stop() {
        if engine.isRunning {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
        }
        audioFile = nil
        emit("mic_stopped", ["samples_written": samplesWritten])
    }
}

// MARK: - System audio recorder ---------------------------------------------

final class SystemAudioRecorder: NSObject, SCStreamOutput, SCStreamDelegate {
    private var stream: SCStream?
    private var audioFile: AVAudioFile?
    private var targetFormat: AVAudioFormat!
    private let outputURL: URL
    private var samplesWritten: Int64 = 0
    private let writeQueue = DispatchQueue(label: "system-audio-write")

    init(outputURL: URL) {
        self.outputURL = outputURL
        super.init()
    }

    func start() async throws {
        // We need a display to construct an SCContentFilter even though we only
        // want audio. ScreenCaptureKit will still pop the Screen Recording
        // permission dialog the first time.
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: false
        )
        guard let display = content.displays.first else {
            throw NSError(domain: "SystemAudioRecorder", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "no display available"])
        }

        let filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])

        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.sampleRate = Int(targetSampleRate)
        config.channelCount = Int(targetChannelCount)
        config.excludesCurrentProcessAudio = true
        // Smallest possible video frames — we're forced to capture some video,
        // but at 1×1 @1fps it's effectively nothing.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        // Set up target PCM format for writing.
        guard let format = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: targetSampleRate,
            channels: targetChannelCount,
            interleaved: true
        ) else {
            throw NSError(domain: "SystemAudioRecorder", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "target format invalid"])
        }
        targetFormat = format
        audioFile = try AVAudioFile(forWriting: outputURL, settings: wavSettings)

        let s = SCStream(filter: filter, configuration: config, delegate: self)
        try s.addStreamOutput(self, type: .audio, sampleHandlerQueue: writeQueue)
        try await s.startCapture()
        self.stream = s
        emit("system_audio_started", ["path": outputURL.path])
    }

    func stop() async throws {
        if let s = stream {
            try await s.stopCapture()
        }
        audioFile = nil
        emit("system_audio_stopped", ["samples_written": samplesWritten])
    }

    // SCStreamOutput
    func stream(_ stream: SCStream,
                didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid else { return }

        // Pull the raw AudioBufferList out of the CMSampleBuffer.
        var blockBuffer: CMBlockBuffer?
        var audioBufferList = AudioBufferList(
            mNumberBuffers: 1,
            mBuffers: AudioBuffer(mNumberChannels: 0, mDataByteSize: 0, mData: nil)
        )

        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: &audioBufferList,
            bufferListSize: MemoryLayout<AudioBufferList>.size,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &blockBuffer
        )
        guard status == noErr, blockBuffer != nil else {
            emitError("CMSampleBuffer→AudioBufferList failed: \(status)")
            return
        }

        // ScreenCaptureKit delivers 32-bit float interleaved samples; describe
        // an AVAudioFormat that matches what's actually in the buffer.
        guard let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(
            sampleBuffer.formatDescription!
        )?.pointee else { return }

        var mutableASBD = asbd
        guard let srcFormat = AVAudioFormat(streamDescription: &mutableASBD) else {
            emitError("AVAudioFormat from ASBD failed")
            return
        }

        let frameCount = AVAudioFrameCount(sampleBuffer.numSamples)
        guard let srcBuf = AVAudioPCMBuffer(pcmFormat: srcFormat,
                                              frameCapacity: frameCount) else { return }
        srcBuf.frameLength = frameCount

        // Copy raw bytes from AudioBufferList into AVAudioPCMBuffer.
        // mutableAudioBufferList lets us update mDataByteSize on the buffer.
        let dstBL = UnsafeMutableAudioBufferListPointer(srcBuf.mutableAudioBufferList)
        if dstBL.count > 0,
           let dst = dstBL[0].mData,
           let src = audioBufferList.mBuffers.mData {
            memcpy(dst, src, Int(audioBufferList.mBuffers.mDataByteSize))
            dstBL[0].mDataByteSize = audioBufferList.mBuffers.mDataByteSize
        }

        // Convert to our target 16 kHz mono int16 PCM.
        guard let converter = AVAudioConverter(from: srcFormat, to: targetFormat) else { return }
        let ratio = targetFormat.sampleRate / srcFormat.sampleRate
        let outCapacity = AVAudioFrameCount(Double(frameCount) * ratio + 1024)
        guard let outBuf = AVAudioPCMBuffer(pcmFormat: targetFormat,
                                              frameCapacity: outCapacity) else { return }

        var convErr: NSError?
        let convStatus = converter.convert(to: outBuf, error: &convErr) { _, statusPtr in
            statusPtr.pointee = .haveData
            return srcBuf
        }
        if convStatus == .error || convErr != nil {
            emitError("system audio convert failed: \(convErr?.localizedDescription ?? "unknown")")
            return
        }

        do {
            try audioFile?.write(from: outBuf)
            samplesWritten += Int64(outBuf.frameLength)
        } catch {
            emitError("system audio write failed: \(error.localizedDescription)")
        }
    }

    // SCStreamDelegate
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        emitError("system audio stream stopped: \(error.localizedDescription)")
    }
}

// MARK: - Heartbeat ----------------------------------------------------------

final class Heartbeat {
    private var timer: Timer?
    let started = Date()

    func start() {
        let t = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            emit("tick", ["elapsed": Date().timeIntervalSince(self.started)])
        }
        RunLoop.main.add(t, forMode: .common)
        self.timer = t
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }
}

// MARK: - Main ---------------------------------------------------------------

guard CommandLine.arguments.count >= 2 else {
    print("usage: recorder <output_dir>")
    exit(1)
}

let outputDir = URL(fileURLWithPath: CommandLine.arguments[1])
do {
    try FileManager.default.createDirectory(at: outputDir, withIntermediateDirectories: true)
} catch {
    emitError("can't create output dir: \(error.localizedDescription)", code: 2)
    exit(2)
}

let systemURL = outputDir.appendingPathComponent("system.wav")
let micURL = outputDir.appendingPathComponent("mic.wav")

let micRecorder = MicRecorder(outputURL: micURL)
let systemRecorder = SystemAudioRecorder(outputURL: systemURL)
let heartbeat = Heartbeat()

// Trap SIGINT for graceful shutdown.
let signalSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
signalSource.setEventHandler {
    Task { @MainActor in
        emit("shutdown_starting")
        heartbeat.stop()
        micRecorder.stop()
        do {
            try await systemRecorder.stop()
        } catch {
            emitError("system audio stop failed: \(error.localizedDescription)")
        }
        emit("shutdown_complete")
        exit(0)
    }
}
signalSource.resume()
signal(SIGINT, SIG_IGN)

// Start everything.
Task { @MainActor in
    do {
        try micRecorder.start()
    } catch {
        emitError("mic start failed: \(error.localizedDescription)", code: 3)
        exit(3)
    }
    do {
        try await systemRecorder.start()
    } catch {
        emitError("system audio start failed: \(error.localizedDescription)", code: 4)
        exit(4)
    }
    heartbeat.start()
    emit("recording_started", [
        "system_path": systemURL.path,
        "mic_path": micURL.path,
    ])
}

// Keep the process alive.
RunLoop.main.run()
