import SwiftUI


struct SpringValue {
    var current: CGFloat = 0
    var target: CGFloat = 0
    var velocity: CGFloat = 0
    var stiffness: CGFloat = 180
    var damping: CGFloat = 16

    mutating func tick(dt: CGFloat) {
        let acc = stiffness * (target - current) - damping * velocity
        velocity += acc * dt
        current += velocity * dt
        if abs(target - current) < 0.005 && abs(velocity) < 0.05 {
            current = target; velocity = 0
        }
    }

    mutating func snap(to value: CGFloat) {
        current = value; target = value; velocity = 0
    }

    mutating func impulse(_ force: CGFloat) { velocity += force }

    mutating func set(_ value: CGFloat, stiffness s: CGFloat? = nil, damping d: CGFloat? = nil) {
        target = value
        if let s { stiffness = s }
        if let d { damping = d }
    }


    var i: Int { Int(round(current)) }

    var f: CGFloat { current }
}


struct TailChain {
    private(set) var angles: [CGFloat]
    private var velocities: [CGFloat]
    let count: Int

    init(segments: Int = 7) {
        count = segments
        angles = Array(repeating: 0, count: segments)
        velocities = Array(repeating: 0, count: segments)
    }

    mutating func update(baseAngle: CGFloat, dt: CGFloat) {
        for i in 0..<count {
            let parent = i == 0 ? baseAngle : angles[i - 1]

            let s = max(60, 200 - CGFloat(i) * 22)
            let d = max(5, 14 - CGFloat(i) * 1.2)
            let force = s * (parent - angles[i]) - d * velocities[i]
            velocities[i] += force * dt
            angles[i] += velocities[i] * dt
        }
    }

    mutating func snap(to angle: CGFloat) {
        for i in 0..<count { angles[i] = angle; velocities[i] = 0 }
    }
}


enum EyeShape: Int {
    case normal = 0
    case happy = 1
    case heart = 2
    case sleepy = 3
    case wide = 4
    case sad = 5
}

enum MouthShape: Int {
    case neutral = 0
    case smile = 1
    case open = 2
    case heart = 3
    case closed = 4
    case frown = 5
}


enum CatPose: Int {
    case frontSitting = 0
    case sideArchedBack = 1
}


func easeOutElastic(_ t: CGFloat) -> CGFloat {
    if t <= 0 { return 0 }; if t >= 1 { return 1 }
    return pow(2, -10 * t) * sin((t * 10 - 0.75) * (.pi * 2 / 3)) + 1
}

func easeOutBack(_ t: CGFloat) -> CGFloat {
    let c1: CGFloat = 1.70158; let c3 = c1 + 1
    return 1 + c3 * pow(t - 1, 3) + c1 * pow(t - 1, 2)
}
