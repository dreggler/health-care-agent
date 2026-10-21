import SwiftUI
import UIKit


private nonisolated func himeDynamic(
    light: (Double, Double, Double),
    dark: (Double, Double, Double)
) -> Color {
    Color(UIColor { trait in
        let c = trait.userInterfaceStyle == .dark ? dark : light
        return UIColor(red: c.0, green: c.1, blue: c.2, alpha: 1)
    })
}

private nonisolated func himeHex(_ hex: UInt32) -> (Double, Double, Double) {
    (Double((hex >> 16) & 0xFF) / 255,
     Double((hex >> 8) & 0xFF) / 255,
     Double(hex & 0xFF) / 255)
}


extension Color {

    static let himeAccent = Color(red: 0.95, green: 0.70, blue: 0.35)
}


enum HimeColor {
    static let accent: Color = .himeAccent

    static let accentStrong: Color = himeDynamic(light: himeHex(0xC78033), dark: himeHex(0xE8A653))

    static let cream: Color = himeDynamic(light: himeHex(0xFFF2DB), dark: himeHex(0x3A2F20))


    static let paper: Color = himeDynamic(light: himeHex(0xF9F5F0), dark: himeHex(0x17130F))
    static let card: Color = himeDynamic(light: himeHex(0xFFFFFF), dark: himeHex(0x241F19))

    static let ink: Color = himeDynamic(light: himeHex(0x2B2520), dark: himeHex(0xF3EDE4))
    static let ink2: Color = himeDynamic(light: himeHex(0x6B6055), dark: himeHex(0xB5AA9C))
    static let line: Color = himeDynamic(light: himeHex(0xE3DCD0), dark: himeHex(0x3A332A))

    static let rose: Color = Color(red: 0xE0 / 255.0, green: 0x80 / 255.0, blue: 0x8A / 255.0)
    static let leaf: Color = Color(red: 0x5A / 255.0, green: 0xAD / 255.0, blue: 0x50 / 255.0)
    static let sky: Color = Color(red: 0x7A / 255.0, green: 0xC4 / 255.0, blue: 0xE8 / 255.0)


    static let ok: Color = himeDynamic(light: himeHex(0x3F8F3A), dark: himeHex(0x5AAD50))
    static let warn: Color = himeDynamic(light: himeHex(0xC78033), dark: himeHex(0xF0B060))
    static let bad: Color = himeDynamic(light: himeHex(0xD0454F), dark: himeHex(0xEE7078))

    static let userBubble: Color = accent
    static let userBubbleText: Color = .white
    static let assistantBubble: Color = card


    static let idle: Color = himeDynamic(light: himeHex(0xD9D4CC), dark: himeHex(0x3E382F))
    static let idleStrong: Color = himeDynamic(light: himeHex(0xB5AFA5), dark: himeHex(0x6A6358))
}


enum HimeRadius {
    static let card: CGFloat = 14
    static let row: CGFloat = 12
    static let bubble: CGFloat = 17
    static let bubbleTail: CGFloat = 5
    static let pill: CGFloat = 18
}
