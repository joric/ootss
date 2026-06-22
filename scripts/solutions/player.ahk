#Requires AutoHotkey v2.0

global solMap := Map()
global solNames := []
global solIndex := 0
global stopFlag := false

; Load CSV: name,sequence per line
Loop Read, "solutions.csv" {
    line := A_LoopReadLine
    if (line = "")
        continue
    parts := StrSplit(line, ",", , 2)
    if (parts.Length = 2) {
        solMap[parts[1]] := parts[2]
        solNames.Push(parts[1])
    }
}

Decode(s) {
    o := "", p := 1
    while p := RegExMatch(s, "(.)(\d*)", &m, p) {
        Loop (m[2] ? m[2] : 1)
            o .= m[1]
        p += m.Len(0)
    }
    return o
}

; Returns true if completed fully, false if cancelled
Play(s) {
    global stopFlag
    stopFlag := false
    WinActivate("ahk_exe sinking_star.exe")
    SetKeyDelay(150)
    decoded := Decode(s)

    Loop Parse, decoded {
        if stopFlag {
            ToolTip("Cancelled")
            SetTimer(() => ToolTip(), -800)
            return false
        }
        SendEvent(A_LoopField)
        Sleep(150)
    }
    return true
}

; Alt+E: play the level at solIndex+1; only advance index if it finishes
!e:: {
    global solIndex, solNames, solMap
    nextIndex := solIndex + 1
    if (nextIndex > solNames.Length) {
        MsgBox("No more lines left in solutions.csv")
        return
    }
    name := solNames[nextIndex]
    ToolTip("Playing: " name)
    completed := Play(solMap[name])
    SetTimer(() => ToolTip(), -1000)

    if completed
        solIndex := nextIndex  ; only advance if fully played
}

; Ctrl+E: set index to a specific level by name (does NOT play)
^e:: {
    global solMap, solNames, solIndex

    pickGui := Gui("+AlwaysOnTop", "Set level")
    pickGui.Add("Text", , "Choose level:")
    ddl := pickGui.Add("DropDownList", "w250 vChoice", solNames)
    ddl.Choose(1)

    okBtn := pickGui.Add("Button", "w100 Default", "OK")
    okBtn.OnEvent("Click", PickLevel)
    pickGui.OnEvent("Close", (*) => pickGui.Destroy())
    pickGui.Show("AutoSize")

    PickLevel(*) {
        saved := pickGui.Submit()  ; reads vChoice etc, hides gui
        name := saved.Choice
        found := false
        for i, n in solNames {
            if (n = name) {
                solIndex := i - 1
                found := true
                break
            }
        }
        if found
            ToolTip("Index set to: " name)
        else
            ToolTip("Level '" name "' not found")
        SetTimer(() => ToolTip(), -1200)
    }
}

; Esc: cancel current execution, but still pass Escape through to the game
~Esc:: {
    global stopFlag
    stopFlag := true
}
