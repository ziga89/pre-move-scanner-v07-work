# Native iOS client

The scanner itself should run on a Mac/VPS/server. iOS can display the live feed,
but iOS will suspend long-running socket/collector work when the app is backgrounded.

## Build
1. Open Xcode on a Mac.
2. Create a new **iOS > App** project named `PreMoveScanner`, Interface: SwiftUI.
3. Replace the generated Swift files with:
   - PreMoveScannerApp.swift
   - Models.swift
   - ScannerSocket.swift
   - ContentView.swift
4. In `ScannerSocket.swift`, change `serverURL`:
   - same Wi-Fi: `ws://YOUR_MAC_LAN_IP:8000/ws`
   - remote/VPS: `wss://YOUR_DOMAIN/ws`
5. For plain HTTP/WS on a LAN, Xcode/iOS may require an App Transport Security exception.
   For normal use, deploy the server behind HTTPS and use `wss://`.

For TestFlight/App Store distribution you need an Apple Developer account.
