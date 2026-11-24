import QtQuick

Text {
    property bool mono: false
    color: Theme.ink
    font.pixelSize: 14
    font.family: mono ? Theme.monoFamily : Theme.fontFamily
    font.weight: Font.Normal
    font.hintingPreference: Font.PreferFullHinting
    renderType: Text.NativeRendering
    elide: Text.ElideRight
    verticalAlignment: Text.AlignVCenter
}
