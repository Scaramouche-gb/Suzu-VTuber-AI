// Quickshell VTuber LayerShell Overlay (for WebEngine / noon-qs-web fork)
// Usage: quickshell -c vtuber (or point to ~/.config/quickshell/vtuber/shell.qml)

import Quickshell
import Quickshell.Wayland
import QtQuick
import QtWebEngine

PanelWindow {
    id: win
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.namespace: "vtuber"
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore

    anchors {
        right: true
        bottom: true
    }
    margins {
        right: 16
        bottom: 0
    }

    implicitWidth: 420
    implicitHeight: 720
    color: "transparent"

    // Click hitbox definition (clicks outside the hitbox pass through)
    mask: Region {
        item: hitbox
    }

    Item {
        id: hitbox
        x: 90
        y: 80
        width: 240
        height: 640
    }

    WebEngineView {
        anchors.fill: parent
        url: "http://127.0.0.1:8765/?fit=bust"
        backgroundColor: "transparent"
        settings.playbackRequiresUserGesture: false
    }
}
