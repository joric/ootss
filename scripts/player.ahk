#Requires AutoHotkey v2.0

; ================= USER CONFIG =================
UP := "w"
LEFT := "a"
DOWN := "s"
RIGHT := "d"
PRIMARY_ACTION := "x"
SWITCH_CHARACTERS := "c"
; =================================================

global solMap := Map()
global solNames := []
global solIndex := 0
global stopFlag := false
global recording := false
global recordBuf := ""
global mainGui := 0
global ddl := 0
global SolutionsPath := "../data/solutions.txt"

global RECORD_KEY := Map(
    "n", UP,
    "w", LEFT,
    "s", DOWN,
    "e", RIGHT,
    "x", PRIMARY_ACTION,
    "c", SWITCH_CHARACTERS
)

global PLAYBACK_KEY := Map(
    "n", "{Up}",
    "w", "{Left}",
    "s", "{Down}",
    "e", "{Right}",
    "x", PRIMARY_ACTION,
    "c", SWITCH_CHARACTERS
)

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

; ---------- Recording ----------
RecordKey(code) {
    global recording, recordBuf
    if recording
        recordBuf .= code
}

; Function param creates a fresh, properly-scoped binding per call,
; unlike a loop variable, which all closures would otherwise share.
MakeRecorder(code) {
    return (*) => RecordKey(code)
}

RegisterRecordingHotkeys() {
    global RECORD_KEY
    for code, key in RECORD_KEY {
        try Hotkey("~" key, MakeRecorder(code))
    }

    ; Cursor keys always count as directions too, regardless of config
    try Hotkey("~Up", MakeRecorder("u"))
    try Hotkey("~Left", MakeRecorder("l"))
    try Hotkey("~Down", MakeRecorder("d"))
    try Hotkey("~Right", MakeRecorder("r"))
}
RegisterRecordingHotkeys()

StartRecording() {
    global recording, recordBuf
    recording := true
    recordBuf := ""
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
    } else {
        ddl.Choose(1)
    }
}

CurrentSelectedName() {
    global ddl
    if !IsObject(ddl)
        return ""
    return NameFromDisplay(ddl.Text)
}

OpenGui(*) {
    global mainGui, ddl, solNames, solIndex
    if IsObject(mainGui) {
        mainGui.Show()
        return
    }
    mainGui := Gui("+AlwaysOnTop", "Solution Player")
    mainGui.Add("Text", , "Level:")
    ddl := mainGui.Add("DropDownList", "w300 vChoice", DisplayList())
    startSel := (solIndex >= 1 && solIndex <= solNames.Length) ? solIndex : 1
    ddl.Choose(startSel)
    ddl.OnEvent("Change", OnDropdownChange)

    playBtn := mainGui.Add("Button", "w90", "Play")
    recBtn := mainGui.Add("Button", "x+10 w90", "Record")
    stopBtn := mainGui.Add("Button", "x+10 w90", "Stop")

    playBtn.OnEvent("Click", (*) => DoPlay())
    recBtn.OnEvent("Click", (*) => DoRecord())
    stopBtn.OnEvent("Click", (*) => DoStop())

    mainGui.OnEvent("Close", (*) => mainGui.Hide())
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
    global solMap
    name := CurrentSelectedName()
    if (name = "")
        return
    ToolTip("Playing: " name)
    Play(solMap[name])
    SetTimer(() => ToolTip(), -1000)
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
}

; ---------- Hotkeys ----------
^!e:: OpenGui()

!e:: {
    global solIndex, solNames, solMap, ddl

    name := ""
    if IsObject(ddl)
        name := CurrentSelectedName()
    if (name = "" && solIndex >= 1 && solIndex <= solNames.Length)
        name := solNames[solIndex]
    if (name = "" && solNames.Length >= 1) {
        name := solNames[1]
        solIndex := 1
    }
    if (name = "") {
        MsgBox("No level selected")
        return
    }

    curIdx := 0
    for i, n in solNames {
        if (n = name) {
            curIdx := i
            break
        }
    }

    ToolTip("Playing: " name)
    completed := Play(solMap[name])
    SetTimer(() => ToolTip(), -1000)

    if completed {
        nextIndex := curIdx + 1
        if (nextIndex > solNames.Length) {
            MsgBox("No more lines left in solutions file")
        } else {
            solIndex := nextIndex
            RefreshDropdown(solNames[nextIndex])
        }
    }
}

^e:: {
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

~Esc:: {
    global stopFlag
    stopFlag := true
}
