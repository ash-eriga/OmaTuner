import QtQuick
import qs.Ui
import qs.Commons
import qs.Commons as Commons
import "Model.js" as Model

// Recovery notices only: abrupt restart, component error and apply failure,
// each a card over a dimmed panel that waits for an explicit answer. The
// 5-second confirmation lives in the panel footer, not here.
Item {
  id: root

  property var daemon: null
  property color rowForeground: Commons.Color.foreground
  property real countdownNow: 0

  signal keepPausedRequested()
  signal noticeDismissed()
  signal reviewRequested()

  Commons.Translations {
    id: strings
    directory: Qt.resolvedUrl("i18n")
  }

  readonly property var notice: daemon && daemon.state && daemon.state.notice ? daemon.state.notice : null
  readonly property bool noticeIsAbrupt: !!notice && notice.type === "abrupt-restart"
  readonly property bool noticeIsError: !!notice && notice.type === "component-error"
  readonly property bool noticeIsFailed: !!notice && notice.type === "apply-failed"
  readonly property bool noticeVisible: noticeIsAbrupt || noticeIsError || noticeIsFailed
  readonly property bool abruptVisible: noticeIsAbrupt
  readonly property bool errorVisible: noticeIsError
  readonly property bool failedVisible: noticeIsFailed
  readonly property bool anyVisible: noticeVisible

  onAnyVisibleChanged: if (anyVisible) forceActiveFocus()

  Keys.priority: Keys.BeforeItem
  Keys.onPressed: function(event) {
    if (event.key === Qt.Key_Escape) {
      if (abruptVisible) { keepPausedRequested(); event.accepted = true }
      else if (errorVisible || failedVisible) { noticeDismissed(); event.accepted = true }
    }
  }

  Rectangle {
    anchors.fill: parent
    color: Util.alpha(Commons.Color.background, 0.55)
    visible: root.noticeVisible

    // A notice is modal: swallow clicks so the panel behind it cannot be
    // operated while it waits for an answer. The dialog cards are declared
    // later, so their buttons stay on top.
    MouseArea {
      anchors.fill: parent
      enabled: root.noticeVisible
      acceptedButtons: Qt.AllButtons
      onClicked: {}
    }
  }

  component DialogCard: Rectangle {
    id: card
    default property alias content: cardColumn.data
    anchors.centerIn: parent
    width: root.width - Style.space(16)
    radius: Style.cornerRadius
    color: Commons.Color.popups.background
    border.width: Math.max(1, Style.normalBorderWidth)
    border.color: Commons.Color.popups.border
    implicitHeight: cardColumn.implicitHeight + Style.space(20)

    Column {
      id: cardColumn
      anchors.top: parent.top
      anchors.topMargin: Style.space(10)
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(10)
      spacing: Style.space(8)
    }
  }

  component CardTitle: Text {
    textFormat: Text.PlainText
    color: root.rowForeground
    font.family: Style.font.family
    font.pixelSize: Style.font.body
    font.bold: true
    wrapMode: Text.WordWrap
    width: parent.width
    horizontalAlignment: Text.AlignHCenter
  }

  component CardBody: Text {
    textFormat: Text.PlainText
    color: Util.alpha(root.rowForeground, 0.85)
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
    wrapMode: Text.WordWrap
    width: parent.width
    horizontalAlignment: Text.AlignHCenter
  }

  component CardActions: Row {
    spacing: Style.space(8)
    anchors.horizontalCenter: parent.horizontalCenter
  }

  // -------------------------------------------------- abrupt restart
  DialogCard {
    visible: root.abruptVisible

    CardTitle {
      text: strings.t("notice.abruptTitle")
    }
    CardBody {
      text: strings.t("notice.abruptBody")
    }
    Text {
      anchors.horizontalCenter: parent.horizontalCenter
      textFormat: Text.PlainText
      text: Model.targ(strings.t("notice.defaultsReturn"),
        root.notice && root.notice.deadlineMs ? Model.countdownSeconds(root.notice.deadlineMs, root.countdownNow) : "0")
      color: Commons.Color.accent
      font.family: Style.font.family
      font.pixelSize: Style.font.title
      font.bold: true
    }
    CardBody {
      text: strings.t("notice.keepPausedKeys")
    }
    Button {
      anchors.horizontalCenter: parent.horizontalCenter
      text: strings.t("notice.keepPaused")
      fontSize: Style.font.caption
      foreground: root.rowForeground
      bordered: true
      onClicked: root.keepPausedRequested()
    }
  }

  // ------------------------------------------------ component error
  DialogCard {
    visible: root.errorVisible

    CardTitle {
      text: Model.targ(strings.t("notice.errorTitle"), root.notice ? root.notice.component.toUpperCase() : "")
    }
    CardBody {
      text: root.notice ? root.notice.message : ""
    }
    CardBody {
      text: strings.t("notice.uncertain")
      visible: !!(root.notice && root.notice.uncertain)
      color: Commons.Color.urgent
    }
    CardActions {
      Button {
        text: strings.t("notice.review")
        fontSize: Style.font.caption
        foreground: Commons.Color.accent
        bordered: true
        onClicked: root.reviewRequested()
      }
      Button {
        text: strings.t("notice.dismiss")
        fontSize: Style.font.caption
        foreground: root.rowForeground
        bordered: true
        onClicked: root.noticeDismissed()
      }
    }
  }

  // ------------------------------------------------- apply failed
  DialogCard {
    visible: root.failedVisible

    CardTitle {
      text: strings.t("notice.applyFailedTitle")
      color: Commons.Color.urgent
    }
    CardBody {
      text: root.notice ? root.notice.message : ""
    }
    CardBody {
      text: strings.t("notice.uncertain")
      visible: !!(root.notice && root.notice.uncertain)
      color: Commons.Color.urgent
    }
    CardActions {
      Button {
        text: strings.t("notice.dismiss")
        fontSize: Style.font.caption
        foreground: root.rowForeground
        bordered: true
        onClicked: root.noticeDismissed()
      }
    }
  }
}
