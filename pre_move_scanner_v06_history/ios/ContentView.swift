import SwiftUI

struct ContentView: View {
    @State private var socket = ScannerSocket()

    var body: some View {
        NavigationStack {
            List {
                if let error = socket.error, !error.isEmpty {
                    Text(error).font(.caption).foregroundStyle(.secondary)
                }
                ForEach(socket.metrics) { m in
                    VStack(alignment: .leading, spacing: 10) {
                        HStack {
                            VStack(alignment: .leading) {
                                Text(m.symbol).font(.headline)
                                Text("$\(m.price, specifier: "%.6f")  ·  5m \(m.price_change_5m_pct, specifier: "%.2f")%")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            Text("\(Int(m.score))/100")
                                .font(.title2.bold())
                        }
                        ProgressView(value: m.score, total: 100)
                        HStack {
                            Stat("Ask ±1%", money(m.ask_depth_1))
                            Stat("Bid ±1%", money(m.bid_depth_1))
                        }
                        HStack {
                            Stat("Buy 60s", "\(Int(m.buy_ratio_60s * 100))%")
                            Stat("Vol accel", String(format: "%.2fx", m.volume_accel))
                        }
                        HStack {
                            Stat("Ask repl.", String(format: "%.2fx", m.ask_replenishment))
                            Stat("Spread", String(format: "%.2f bps", m.spread_bps))
                        }
                    }
                    .padding(.vertical, 6)
                }
            }
            .navigationTitle("Pre‑Move Scanner")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Circle()
                        .fill(socket.connected ? .green : .gray)
                        .frame(width: 10, height: 10)
                }
            }
        }
        .onAppear { socket.connect() }
        .onDisappear { socket.disconnect() }
    }

    @ViewBuilder
    private func Stat(_ name: String, _ value: String) -> some View {
        VStack(alignment: .leading) {
            Text(name).font(.caption2).foregroundStyle(.secondary)
            Text(value).font(.subheadline.bold())
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func money(_ value: Double) -> String {
        if value >= 1_000_000 { return String(format: "$%.2fM", value / 1_000_000) }
        if value >= 1_000 { return String(format: "$%.1fK", value / 1_000) }
        return String(format: "$%.0f", value)
    }
}
