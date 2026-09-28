import Foundation
import Observation

@Observable
final class ScannerSocket {
    var metrics: [ScannerMetric] = []
    var connected = false
    var error: String?

    // Change this to wss://your-domain.example/ws for remote use.
    var serverURL = URL(string: "ws://127.0.0.1:8000/ws")!

    private var task: URLSessionWebSocketTask?

    func connect() {
        task?.cancel(with: .goingAway, reason: nil)
        task = URLSession.shared.webSocketTask(with: serverURL)
        task?.resume()
        connected = true
        receive()
        ping()
    }

    func disconnect() {
        task?.cancel(with: .normalClosure, reason: nil)
        connected = false
    }

    private func receive() {
        task?.receive { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                switch result {
                case .failure(let err):
                    self.connected = false
                    self.error = err.localizedDescription
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.connect() }
                case .success(let msg):
                    let data: Data?
                    switch msg {
                    case .string(let text): data = text.data(using: .utf8)
                    case .data(let d): data = d
                    @unknown default: data = nil
                    }
                    if let data,
                       let env = try? JSONDecoder().decode(ScannerEnvelope.self, from: data) {
                        self.metrics = env.symbols.values.sorted { $0.score > $1.score }
                        self.error = env.last_error
                    }
                    self.receive()
                }
            }
        }
    }

    private func ping() {
        task?.sendPing { [weak self] err in
            guard let self else { return }
            if let err { self.error = err.localizedDescription }
            DispatchQueue.main.asyncAfter(deadline: .now() + 15) { self.ping() }
        }
    }
}
