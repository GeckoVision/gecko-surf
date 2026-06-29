# Produce a crisp terminal GIF of `gecko serve` on a real API — no live recording.
#
#   1. Install vhs (https://github.com/charmbracelet/vhs):
#        macOS:  brew install vhs
#        Linux:  see the releases page (needs ttyd + ffmpeg)
#   2. From the repo root:  vhs demo/gecko-demo.tape   →  writes demo/gecko-serve.gif
#
# vhs runs the command in a real terminal and records it — perfectly timed, no retakes.

Output demo/gecko-serve.gif

Set FontSize 16
Set Width 1200
Set Height 640
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms

Type "uvx --from 'gecko-surf[serve]' gecko https://petstore3.swagger.io/api/v3/openapi.json --mode recorded"
Sleep 700ms
Enter
Sleep 5s
Ctrl+C
Sleep 1500ms
