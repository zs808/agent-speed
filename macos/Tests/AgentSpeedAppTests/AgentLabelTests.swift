import CoreGraphics
import Testing
@testable import AgentSpeed

@Suite("Dashboard agent labels")
struct AgentLabelTests {
    @Test func onlyOverflowingLabelsScroll() {
        #expect(AgentLabelScroll.distance(contentWidth: 150, viewportWidth: 200, reduceMotion: false) == 0)
        #expect(AgentLabelScroll.distance(contentWidth: 200.5, viewportWidth: 200, reduceMotion: false) == 0)
        #expect(AgentLabelScroll.distance(contentWidth: 420, viewportWidth: 200, reduceMotion: false) == 220)
    }

    @Test func reduceMotionAndInvalidMeasurementsKeepTheLabelStill() {
        #expect(AgentLabelScroll.distance(contentWidth: 420, viewportWidth: 200, reduceMotion: true) == 0)
        #expect(AgentLabelScroll.distance(contentWidth: 420, viewportWidth: 0, reduceMotion: false) == 0)
        #expect(AgentLabelScroll.distance(contentWidth: .infinity, viewportWidth: 200, reduceMotion: false) == 0)
    }

    @Test func rowsOutsideTheListViewportDoNotAnimate() {
        #expect(AgentLabelScroll.isVisible(top: 10, bottom: 30, viewportHeight: 100))
        #expect(AgentLabelScroll.isVisible(top: -10, bottom: 10, viewportHeight: 100))
        #expect(!AgentLabelScroll.isVisible(top: -20, bottom: 0, viewportHeight: 100))
        #expect(!AgentLabelScroll.isVisible(top: 100, bottom: 120, viewportHeight: 100))
        #expect(!AgentLabelScroll.isVisible(top: 0, bottom: 20, viewportHeight: 0))
    }

    @Test func tooltipContainsTheUnabridgedProjectAndModel() {
        let project = "a-very-long-project-name-that-cannot-fit-in-the-dashboard"
        let detail = "an-extraordinarily-long-model-name · Codex"
        let text = AgentRowText.tooltip(project: project, detail: detail, status: "Working", traffic: "↓ 5 MB · ↑ 2 MB", mode: "Fast")
        #expect(text.contains(project))
        #expect(text.contains(detail))
        #expect(text.contains("Working"))
        #expect(text.contains("↓ 5 MB · ↑ 2 MB"))
        #expect(text.contains("Fast"))
    }

    @Test func tooltipAvoidsEmptyLinesForMissingDetails() {
        let text = AgentRowText.tooltip(project: "project", detail: "", status: "Idle", traffic: nil, mode: "")
        #expect(text == "project · Idle")
    }
}
