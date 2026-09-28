import Foundation

struct ScannerEnvelope: Codable {
    let exchange: String?
    let last_error: String?
    let symbols: [String: ScannerMetric]
    let server_ts: Double?
}

struct ScannerMetric: Codable, Identifiable {
    var id: String { symbol }
    let symbol: String
    let price: Double
    let spread_bps: Double
    let bid_depth_1: Double
    let ask_depth_1: Double
    let buy_ratio_60s: Double
    let volume_60s: Double
    let volume_accel: Double
    let price_change_5m_pct: Double
    let ask_replenishment: Double
    let ask_depth_ratio_vs_baseline: Double
    let score: Double
    let baseline_samples: Int
    let book_ready: Bool
}
