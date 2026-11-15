import QtQuick
import Quickshell
import Quickshell.Io

// Client for the omatuner daemon: newline-delimited JSON over a unix socket.
// Holds the last state snapshot, delivers replies to callers, and resubscribes
// telemetry whenever the connection is rebuilt. Retries alternate between the
// user-session socket and the system service socket so the same plugin works
// before and after `bin/install`.
Item {
  id: root

  property var state: null
  property var telemetry: ({})
  property bool telemetryWanted: false
  property string lastError: ""
  readonly property bool online: socket.connected && state !== null

  property var _requests: ({})
    property int _attempt: 0

  function candidatePath() {
    const override = Quickshell.env("OMATUNER_SOCKET")
    if (override) return override
    const runtime = Quickshell.env("XDG_RUNTIME_DIR")
    if (_attempt % 2 === 0 && runtime) return runtime + "/omatuner.sock"
    return "/run/omatuner/omatuner.sock"
  }

  function call(method, params, callback) {
    // Writing to a closed Socket drops the frame silently, so the callback
    // would wait forever and the caller would never show its error.
    if (!socket.connected) {
      if (callback) callback({ ok: false, error: { code: "offline", message: "no daemon connection" } })
      return
    }
    const id = Math.floor(Math.random() * 1e9)
    _requests[id] = callback || null
    socket.write(JSON.stringify({ id: id, method: method, params: params || {} }) + "\n")
    socket.flush()
  }

  function setTelemetryWanted(wanted) {
    telemetryWanted = wanted
    if (socket.connected) call("subscribe", { telemetry: wanted })
  }

  // `path` is assigned, never bound: a binding here re-evaluated whenever
  // _attempt changed, which is what the shell reported as "Binding loop
  // detected for property path" and left the socket pointing at whichever
  // candidate won the loop instead of the one that answered.
  function connectFirst() {
    socket.path = candidatePath()
    socket.connected = true
  }

  function retry() {
    _attempt++
    connectFirst()
  }

  Socket {
    id: socket
    path: ""
    connected: false

    parser: SplitParser {
      splitMarker: "\n"

      onRead: function(line) {
        let frame
        try {
          frame = JSON.parse(line)
        } catch (e) {
          return
        }
        if (frame.event === "state") {
          root.state = frame.data
          return
        }
        if (frame.event === "telemetry") {
          root.telemetry = frame.data
          return
        }
        if (frame.id !== undefined && frame.id in root._requests) {
          const callback = root._requests[frame.id]
          delete root._requests[frame.id]
          if (callback) callback(frame)
        }
      }
    }

    onConnectedChanged: {
      if (connected) {
        root._attempt = 0
        root.lastError = ""
        root.call("hello", {}, frame => {
          if (frame.ok) {
            root.state = frame.result.state
            root.lastError = ""
          } else {
            root.lastError = frame.error ? frame.error.message : "hello failed"
          }
        })
        root.call("subscribe", { telemetry: root.telemetryWanted })
      } else {
        root.state = null
        // A daemon restart kills the connection with requests in flight; their
        // callbacks would never fire, so answer them now with the honest error.
        const pending = root._requests
        root._requests = ({})
        for (const id in pending) {
          const callback = pending[id]
          if (callback) callback({ ok: false, error: { code: "offline", message: "the daemon connection dropped" } })
        }
        reconnectTimer.restart()
      }
    }

    // A failed attempt never flips `connected`, so the change handler above
    // stays silent; the error signal is what covers that case.
    onError: reconnectTimer.restart()

    Component.onCompleted: root.connectFirst()
  }

  Timer {
    id: reconnectTimer
    // Back off while the daemon is absent: a fixed 1.5s retry spammed the
    // journal with socket errors whenever the chip ran without a daemon.
    interval: Math.min(1500 * Math.pow(1.7, Math.min(root._attempt, 8)), 20000)
    repeat: false
    onTriggered: {
      root.retry()
    }
  }
}
