#Requires AutoHotkey v2.0

; ================= USER CONFIG =================
UP := "w"
LEFT := "a"
DOWN := "s"
RIGHT := "d"
PRIMARY_ACTION := "x"
SWITCH_CHARACTERS := "c"
SECONDARY_ACTION := "v"
UNDO := "z"

; Record/play codes
CODE_UP := "U"
CODE_LEFT := "L"
CODE_DOWN := "D"
CODE_RIGHT := "R"
CODE_ACTION := "X"
CODE_SWITCH := "C"
CODE_SECONDARY := "V"

; Physical arrow keys (scan codes, layout-independent)
UP_ARROW := "sc148"
LEFT_ARROW := "sc14B"
DOWN_ARROW := "sc150"
RIGHT_ARROW := "sc14D"
; =================================================

global solMap := Map()
global solNames := []
global solIndex := 0
global stopFlag := false
global recording := false
global recordBuf := ""
global BASE_TITLE := "Solution Player"
global mainGui := 0
global ddl := 0
global playBtn := 0
global recBtn := 0
global stopBtn := 0
global SolutionsPath := "../data/solutions.txt"

global RECORD_KEY := Map(
    CODE_UP, UP,
    CODE_LEFT, LEFT,
    CODE_DOWN, DOWN,
    CODE_RIGHT, RIGHT,
    CODE_ACTION, PRIMARY_ACTION,
    CODE_SWITCH, SWITCH_CHARACTERS,
    CODE_SECONDARY, SECONDARY_ACTION
)

global PLAYBACK_KEY := Map(
    CODE_UP, "{Up}",
    CODE_LEFT, "{Left}",
    CODE_DOWN, "{Down}",
    CODE_RIGHT, "{Right}",
    CODE_ACTION, PRIMARY_ACTION,
    CODE_SWITCH, SWITCH_CHARACTERS,
    CODE_SECONDARY, SECONDARY_ACTION
)

; ---------- UI State ----------
SetGuiTitle(title := "") {
    global mainGui, BASE_TITLE
    if !IsObject(mainGui)
        return
    mainGui.Title := (title = "") ? BASE_TITLE : title
}

SetIdleUI() {
    global playBtn, recBtn, stopBtn
    if IsObject(playBtn)
        playBtn.Enabled := true
    if IsObject(recBtn)
        recBtn.Enabled := true
    if IsObject(stopBtn)
        stopBtn.Enabled := false
    SetGuiTitle()
}

SetRecordingUI() {
    global playBtn, recBtn, stopBtn
    if IsObject(playBtn)
        playBtn.Enabled := false
    if IsObject(recBtn)
        recBtn.Enabled := false
    if IsObject(stopBtn)
        stopBtn.Enabled := true
    SetGuiTitle("Recording...")
}

SetPlayingUI() {
    global playBtn, recBtn, stopBtn
    if IsObject(playBtn)
        playBtn.Enabled := false
    if IsObject(recBtn)
        recBtn.Enabled := false
    if IsObject(stopBtn)
        stopBtn.Enabled := true
    SetGuiTitle("Playing...")
}

; ---------- Load ----------
LoadSolutions() {
    global solMap, solNames
    solMap := Map()
    solNames := []
    Loop Read, SolutionsPath {
        line := A_LoopReadLine
        if (line = "")
            continue
        parts := StrSplit(line, ":", , 2)
        if (parts.Length >= 1) {
            name := parts[1]
            sol := (parts.Length = 2) ? parts[2] : ""
            solMap[name] := sol
            solNames.Push(name)
        }
    }
}
LoadSolutions()

; ---------- Save ----------
SaveSolutions() {
    global solMap, solNames
    out := ""
    for i, name in solNames {
        out .= name ":" solMap[name] "`n"
    }
    f := FileOpen(SolutionsPath, "w")
    f.Write(out)
    f.Close()
}

; ---------- RLE ----------
Decode(s) {
    o := "", p := 1
    while p := RegExMatch(s, "(.)(\d*)", &m, p) {
        Loop (m[2] ? m[2] : 1)
            o .= m[1]
        p += m.Len(0)
    }
    return o
}
Encode(s) {
    if (s = "")
        return ""
    out := ""
    cur := SubStr(s, 1, 1)
    cnt := 1
    len := StrLen(s)
    Loop len - 1 {
        c := SubStr(s, A_Index + 1, 1)
        if (c = cur) {
            cnt++
        } else {
            out .= cur (cnt > 1 ? cnt : "")
            cur := c
            cnt := 1
        }
    }
    out .= cur (cnt > 1 ? cnt : "")
    return out
}

; ---------- Display helpers ----------
DisplayList() {
    global solNames, solMap
    arr := []
    for i, name in solNames
        arr.Push(name " (" StrLen(solMap[name]) ")")
    return arr
}
NameFromDisplay(text) {
    return RegExReplace(text, " \(\d+\)$", "")
}
FirstUnsolvedIndex() {
    global solNames, solMap
    for i, name in solNames {
        if (solMap[name] = "")
            return i
    }
    return 1
}

; ---------- Playback ----------
SendCode(code) {
    global PLAYBACK_KEY
    keyStr := PLAYBACK_KEY[code]
    if (StrLen(keyStr) = 1) {
        SendEvent("{Raw}" keyStr)
    } else {
        SendEvent(keyStr)
    }
}

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
        SendCode(A_LoopField)
        Sleep(150)
    }
    return true
}

; ---------- Shared play helpers ----------
GetActiveLevelName() {
    global solIndex, solNames, ddl

    name := ""
    if IsObject(ddl)
        name := CurrentSelectedName()
    if (name = "" && solIndex >= 1 && solIndex <= solNames.Length)
        name := solNames[solIndex]
    if (name = "" && solNames.Length >= 1) {
        name := solNames[solIndex >= 1 ? solIndex : FirstUnsolvedIndex()]
        if (solIndex < 1 || solIndex > solNames.Length)
            solIndex := FirstUnsolvedIndex()
    }
    return name
}

GetLevelIndexByName(name) {
    global solNames
    for i, n in solNames
        if (n = name)
            return i
    return 0
}

PlayCurrent(advance := false) {
    global solIndex, solNames, solMap

    name := GetActiveLevelName()
    if (name = "") {
        MsgBox("No level selected")
        return
    }

    curIdx := GetLevelIndexByName(name)

    SetPlayingUI()
    ToolTip("Playing: " name)
    completed := Play(solMap[name])
    SetIdleUI()
    SetTimer(() => ToolTip(), -1000)

    if (advance && completed) {
        nextIndex := curIdx + 1
        if (nextIndex > solNames.Length) {
            MsgBox("No more lines left in solutions file")
        } else {
            solIndex := nextIndex
            RefreshDropdown(solNames[nextIndex])
        }
    }
}

; ---------- Recording ----------
RecordKey(code) {
    global recording, recordBuf
    if recording
        recordBuf .= code
}

MakeRecorder(code) {
    return (*) => RecordKey(code)
}

RegisterRecordingHotkeys() {
    global RECORD_KEY
    global UP_ARROW, LEFT_ARROW, DOWN_ARROW, RIGHT_ARROW
    global CODE_UP, CODE_LEFT, CODE_DOWN, CODE_RIGHT

    for code, key in RECORD_KEY {
        try Hotkey("~*" key, MakeRecorder(code))
    }

    ; Physical arrows (layout-independent), code from defines
    try Hotkey("~*" UP_ARROW, MakeRecorder(CODE_UP))
    try Hotkey("~*" LEFT_ARROW, MakeRecorder(CODE_LEFT))
    try Hotkey("~*" DOWN_ARROW, MakeRecorder(CODE_DOWN))
    try Hotkey("~*" RIGHT_ARROW, MakeRecorder(CODE_RIGHT))
}
RegisterRecordingHotkeys()

StartRecording() {
    global recording, recordBuf
    recording := true
    recordBuf := ""
    SetRecordingUI()
    ToolTip("Recording...")
}

StopRecordingAndSave(name) {
    global recording, recordBuf, solMap
    if !recording
        return
    recording := false
    encoded := Encode(recordBuf)
    solMap[name] := encoded
    SaveSolutions()
    RefreshDropdown(name)
    SetIdleUI()
    ToolTip("Saved " name ": " encoded)
    SetTimer(() => ToolTip(), -1200)
}

; ---------- GUI ----------
RefreshDropdown(selectName := "") {
    global ddl, solNames
    if !IsObject(ddl)
        return
    ddl.Delete()
    ddl.Add(DisplayList())
    if (selectName != "") {
        for i, n in solNames {
            if (n = selectName) {
                ddl.Choose(i)
                break
            }
        }
    } else if (solNames.Length >= 1) {
        ddl.Choose(FirstUnsolvedIndex())
    }
}

CurrentSelectedName() {
    global ddl
    if !IsObject(ddl)
        return ""
    return NameFromDisplay(ddl.Text)
}

OpenGui(*) {
    global mainGui, ddl, solNames, solIndex, playBtn, recBtn, stopBtn, BASE_TITLE
    if IsObject(mainGui) {
        mainGui.Show()
        return
    }
    mainGui := Gui("+AlwaysOnTop", BASE_TITLE)
    mainGui.Add("Text", , "Level:")
    ddl := mainGui.Add("DropDownList", "w300 vChoice", DisplayList())
    startSel := (solIndex >= 1 && solIndex <= solNames.Length) ? solIndex : FirstUnsolvedIndex()
    if (solNames.Length >= 1)
        ddl.Choose(startSel)
    solIndex := startSel
    ddl.OnEvent("Change", OnDropdownChange)

    playBtn := mainGui.Add("Button", "w90", "Play")
    recBtn := mainGui.Add("Button", "x+10 w90", "Record")
    stopBtn := mainGui.Add("Button", "x+10 w90", "Stop")

    playBtn.OnEvent("Click", (*) => DoPlay())
    recBtn.OnEvent("Click", (*) => DoRecord())
    stopBtn.OnEvent("Click", (*) => DoStop())
    mainGui.OnEvent("Close", (*) => ExitApp())

    SetIdleUI()
    mainGui.Show("AutoSize")
}

OnDropdownChange(*) {
    global ddl, solNames, solIndex
    name := NameFromDisplay(ddl.Text)
    for i, n in solNames {
        if (n = name) {
            solIndex := i
            break
        }
    }
}

DoPlay(*) {
    PlayCurrent(false)
}

DoRecord(*) {
    StartRecording()
}

DoStop(*) {
    global stopFlag, recording
    stopFlag := true
    name := CurrentSelectedName()
    if (recording && name != "")
        StopRecordingAndSave(name)
    else
        SetIdleUI()
}

RecordSolution(*) {
    global recording, solIndex, solNames, ddl
    if recording {
        name := IsObject(ddl) ? CurrentSelectedName() : ""
        if (name = "" && solIndex >= 1 && solIndex <= solNames.Length)
            name := solNames[solIndex]
        if (name = "") {
            ToolTip("No level selected to save to")
            SetTimer(() => ToolTip(), -1200)
            return
        }
        StopRecordingAndSave(name)
    } else {
        StartRecording()
    }
}

global stepIndex := 0
global stepSequence := ""
global stepActive := false

StartStepPlayback() {
    global stepIndex, stepSequence, stepActive, solMap, solIndex, solNames, ddl
    name := GetActiveLevelName()
    if (name = "") {
        ToolTip("No level selected")
        SetTimer(() => ToolTip(), -1200)
        return
    }
    stepSequence := Decode(solMap[name])
    if (stepSequence = "") {
        ToolTip("No solution recorded")
        SetTimer(() => ToolTip(), -1200)
        return
    }
    stepIndex := 0
    stepActive := true
    WinActivate("ahk_exe sinking_star.exe")
    ToolTip("Step mode: Press ] for next move")
    SetTimer(() => ToolTip(), -1500)
}

StepNextMove() {
    global stepIndex, stepSequence, stepActive
    if !stepActive {
        StartStepPlayback()
        return
    }
    if (stepIndex >= StrLen(stepSequence)) {
        ToolTip("End of solution!")
        SetTimer(() => ToolTip(), -1200)
        stepActive := false
        return
    }
    stepIndex++
    SendCode(SubStr(stepSequence, stepIndex, 1))
    ToolTip("Move " stepIndex "/" StrLen(stepSequence))
    SetTimer(() => ToolTip(), -800)
}


RewindSteps() {
    global stepIndex, stepActive, stepSequence
    if stepActive {
        stepIndex := 0
        ToolTip("Rewound to start (0/" StrLen(stepSequence) ")")
        SetTimer(() => ToolTip(), -1200)
    } else {
        ToolTip("No step mode active")
        SetTimer(() => ToolTip(), -800)
    }
}

; ---------- Hotkeys ----------
^!e:: OpenGui()           ; Ctrl+Alt+E: Open GUI
^+e:: PlayCurrent(true)   ; Ctrl+Shift+E: Play + select next level
!e::  PlayCurrent(false)  ; Alt+ E: Play only
^e::  RecordSolution()    ; Ctrl+E: record solution

~![::Send(UNDO)           ; Alt + [: undo
~!]::StepNextMove()       ; Alt + ]: step forward
~r::RewindSteps()         ; R: rewind steps

~Esc:: {
    global stopFlag
    stopFlag := true
}

OpenGui()
