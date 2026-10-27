import SwiftUI

/// The project and model/source labels share the available space; only an overflowing identity moves.
struct AgentIdentityLabel: View {
    let project: String
    let detail: String
    let trafficSuffix: String?
    let projectAccessibilityLabel: String
    let reduceMotion: Bool

    @State private var contentWidth: CGFloat = 0
    @State private var offset: CGFloat = 0

    var body: some View {
        GeometryReader { geometry in
            let distance = AgentLabelScroll.distance(
                contentWidth: contentWidth, viewportWidth: geometry.size.width, reduceMotion: reduceMotion
            )
            let request = ScrollRequest(project: project, detail: detail, distance: distance)

            Group {
                if distance > 0 {
                    labels
                        .fixedSize(horizontal: true, vertical: false)
                        .offset(x: -offset)
                } else {
                    labels
                }
            }
            .frame(width: geometry.size.width, height: DashboardLayout.rowHeight, alignment: .leading)
            .clipped()
            .background(alignment: .leading) {
                // Measure ideal text width separately so the clipped view never changes the row's layout while moving.
                labels
                    .fixedSize(horizontal: true, vertical: false)
                    .hidden()
                    .accessibilityHidden(true)
                    .onGeometryChange(for: CGFloat.self) { ($0.size.width * 2).rounded() / 2 } action: { width in
                        if width != contentWidth { contentWidth = width }
                    }
            }
            .task(id: request) { await scroll(distance: distance) }
        }
        .frame(height: DashboardLayout.rowHeight)
    }

    private var labels: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(project)
                .font(DashboardFont.rowTitle)
                .lineLimit(1)
                .layoutPriority(1)
                .accessibilityLabel(projectAccessibilityLabel)
            HStack(spacing: 0) {
                Text(detail)
                    .lineLimit(1)
                    .truncationMode(.tail)
                if let trafficSuffix {
                    Text(trafficSuffix)
                        .lineLimit(1)
                        .fixedSize()
                }
            }
            .font(DashboardFont.detail)
            .foregroundStyle(.dashboardSecondary)
        }
    }

    @MainActor
    private func scroll(distance: CGFloat) async {
        var reset = Transaction(animation: nil)
        reset.disablesAnimations = true
        withTransaction(reset) { offset = 0 }
        guard distance > 0 else { return }

        let duration = Double(distance) / AgentLabelScroll.pointsPerSecond
        do {
            while !Task.isCancelled {
                try await Task.sleep(for: .seconds(AgentLabelScroll.pause))
                withAnimation(.linear(duration: duration)) { offset = distance }
                try await Task.sleep(for: .seconds(duration + AgentLabelScroll.pause))
                withAnimation(.linear(duration: duration)) { offset = 0 }
                try await Task.sleep(for: .seconds(duration))
            }
        } catch is CancellationError {
            // SwiftUI cancels this task when the row disappears or its text and width change.
        } catch {
            return
        }
    }

    private struct ScrollRequest: Equatable {
        let project: String
        let detail: String
        let distance: CGFloat
    }
}

enum AgentLabelScroll {
    static let pointsPerSecond = 28.0
    static let pause = 1.3

    static func distance(contentWidth: CGFloat, viewportWidth: CGFloat, reduceMotion: Bool) -> CGFloat {
        guard !reduceMotion, contentWidth.isFinite, viewportWidth.isFinite, viewportWidth > 1 else { return 0 }
        let overflow = contentWidth - viewportWidth
        return overflow > 1 ? (overflow * 2).rounded() / 2 : 0
    }

    static func isVisible(top: CGFloat, bottom: CGFloat, viewportHeight: CGFloat) -> Bool {
        guard top.isFinite, bottom.isFinite, viewportHeight.isFinite, viewportHeight > 0, bottom > top else { return false }
        return bottom > 0 && top < viewportHeight
    }
}
