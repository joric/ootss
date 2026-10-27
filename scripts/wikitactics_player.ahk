#Requires AutoHotkey v2.0
#SingleInstance Force

; ---------------------------------------------------------------------------
; Config
; ---------------------------------------------------------------------------
TARGET_WIN     := "ahk_exe sinking_star.exe"
RESET_KEY      := "r"
START_OFFSET_MS := 0
DEFAULT_DELTA_TICKS := 0
DEFAULT_LEVEL  := "promesst1_streamlined"
DEFAULT_4X     := false
DEFAULT_SLOW   := false   ; hold Ctrl: 4x longer wall-clock, same ticks
DEFAULT_SHIFT  := false   ; hold Shift: character 2x, ticks unchanged

KEY_UP        := "{Up}"
KEY_DOWN      := "{Down}"
KEY_LEFT      := "{Left}"
KEY_RIGHT     := "{Right}"
KEY_PRIMARY   := "x"
KEY_SECONDARY := "v"
KEY_SWITCH    := "c"
KEY_F         := "f"

TICK_US        := 1
DEFAULT_GAP_MS := 0
PRESS_MS       := 16
PROCESS_PRIORITY := "High"

global g_Abort := false, g_Playing := false, g_ExitRequested := false
global g_LB := "", g_Fast := "", g_Slow := "", g_Shift2 := "", g_Offset := "", g_Delta := "", g_Status := ""
global g_Recs := [], g_File := ""

; ---------------------------------------------------------------------------
; JSON loading (plain InStr/SubStr only)
; ---------------------------------------------------------------------------
ReadValue(text, p, &endp) {
    len := StrLen(text)
    endp := 0
    while (p <= len && InStr(" `t`r`n", SubStr(text, p, 1)))
        p++
    if (SubStr(text, p, 1) != ":")
        return ""
    p++
    while (p <= len && InStr(" `t`r`n", SubStr(text, p, 1)))
        p++
    if (SubStr(text, p, 1) != '"')
        return ""
    start := p + 1
    q := InStr(text, '"', true, start)
    while (q && SubStr(text, q - 1, 1) = "\")
        q := InStr(text, '"', true, q + 1)
    if !q
        return ""
    endp := q + 1
    v := SubStr(text, start, q - start)
    v := StrReplace(v, "\`"", "`"")
    v := StrReplace(v, "\/", "/")
    v := StrReplace(v, "\\", "\")
    return v
}

FindKey(text, key, from, to, &val, &endp) {
    pat := '"' key '"'
    kl := StrLen(pat)
    pos := from
    val := ""
    endp := 0
    while (np := InStr(text, pat, true, pos)) {
        if (to && np >= to)
            return 0
        v := ReadValue(text, np + kl, &e)
        if e {
            val := v
            endp := e
            return np
        }
        pos := np + kl
    }
    return 0
}

LoadSolutions() {
    global g_File
    g_File := A_ScriptDir "\solutions_debug.json"
    if !FileExist(g_File)
        throw Error("Solutions file not found: " g_File)
    text := FileRead(g_File, "UTF-8")

    starts := [], names := [], nends := [], pos := 1
    while (np := FindKey(text, "name", pos, 0, &nm, &ne)) {
        starts.Push(np)
        names.Push(nm)
        nends.Push(ne)
        pos := ne
    }
    if (names.Length = 0)
        throw Error("No records with a name found in " g_File)

    recs := []
    for i, rn in names {
        from := nends[i]
        stop := (i < names.Length) ? starts[i + 1] : StrLen(text) + 1
        region := SubStr(text, from, stop - from)
        FindKey(region, "moves", 1, 0, &mv, &e1)
        FindKey(region, "order", 1, 0, &od, &e2)
        FindKey(region, "frame_ticks", 1, 0, &ft, &e3)
        recs.Push(Map("name", rn, "moves", mv, "order", od, "ticks", ft))
    }
    return recs
}

; ---------------------------------------------------------------------------
; GUI
; ---------------------------------------------------------------------------
BuildGui() {
    global g_LB, g_Fast, g_Slow, g_Shift2, g_Offset, g_Delta, g_Status
    global START_OFFSET_MS, DEFAULT_DELTA_TICKS, DEFAULT_4X, DEFAULT_SLOW, DEFAULT_SHIFT

    g := Gui("+Resize", "WikiTactics Player")
    g.SetFont("s10", "Segoe UI")
    g.MarginX := 10
    g.MarginY := 10

    g.Add("Text", , "Levels:")
    g_LB := g.Add("ListBox", "w420 h400")
    g_LB.OnEvent("DoubleClick", (*) => PlaySelected())

    g_Fast := g.Add("Checkbox", "xm" (DEFAULT_4X ? " Checked" : ""),
        "4x speed (holds Alt before reset and during play)")
    g_Slow := g.Add("Checkbox", "xm" (DEFAULT_SLOW ? " Checked" : ""),
        "Slow down 4x (holds Ctrl; wall-clock x4, same ticks)")
    g_Shift2 := g.Add("Checkbox", "xm" (DEFAULT_SHIFT ? " Checked" : ""),
        "Speed up character 2x (holds Shift; ticks unchanged)")

    g.Add("Text", "xm y+8", "Start offset after reset (ms, game time):")
    g_Offset := g.Add("Edit", "x+8 w70 Number", START_OFFSET_MS)
    g.Add("Text", "xm y+8", "Move delta (ticks, can be negative):")
    g_Delta := g.Add("Edit", "x+8 w90", DEFAULT_DELTA_TICKS)

    btnPlay   := g.Add("Button", "xm y+10 w100 Default", "Play")
    btnStop   := g.Add("Button", "w100 x+10", "Stop")
    btnReload := g.Add("Button", "w120 x+10", "Reload list")
    btnClose  := g.Add("Button", "w100 x+10", "Close")

    btnPlay.OnEvent("Click",   (*) => PlaySelected())
    btnStop.OnEvent("Click",   (*) => StopPlayback())
    btnReload.OnEvent("Click", (*) => RefreshList())
    btnClose.OnEvent("Click",  (*) => RequestExit())

    g_Status := g.Add("StatusBar", , "Ready.")
    g.OnEvent("Close",  (*) => RequestExit())
    g.OnEvent("Escape", (*) => RequestExit())

    g.Show("w560")
    return g
}

RefreshList() {
    global g_LB, g_Recs, g_Status, g_File, DEFAULT_LEVEL
    g_Status.SetText("Loading...")
    try
        recs := LoadSolutions()
    catch as e {
        g_Status.SetText("Load error - see message box")
        MsgBox("Could not load solutions:`n`n" e.Message, "WikiTactics Player", "Iconx")
        return
    }

    names := []
    for rec in recs
        names.Push(rec["name"])

    g_Recs := recs
    g_LB.Delete()
    g_LB.Add(names)

    sel := 0
    if (DEFAULT_LEVEL != "") {
        for i, n in names {
            if (n == DEFAULT_LEVEL) {
                sel := i
                break
            }
        }
        if (sel = 0) {
            for i, n in names {
                if InStr(n, DEFAULT_LEVEL) {
                    sel := i
                    break
                }
            }
        }
    }
    g_LB.Choose(sel > 0 ? sel : 1)
    g_Status.SetText("Loaded " names.Length " level(s) from " g_File
        . (sel > 0 ? "  — selected: " names[sel] : ""))
}

PlaySelected() {
    global g_LB, g_Recs, g_Status, g_Playing
    if g_Playing {
        g_Status.SetText("Already playing. Press Stop first.")
        return
    }
    idx := g_LB.Value
    if (idx < 1 || idx > g_Recs.Length) {
        g_Status.SetText("Select a level first.")
        return
    }
    ReplayLevel(g_Recs[idx])
}

StopPlayback() {
    global g_Abort, g_Status, g_Playing
    if !g_Playing
        return
    g_Abort := true
    g_Status.SetText("Stopping and releasing keys...")
}

RequestExit() {
    global g_Playing, g_ExitRequested
    if g_Playing {
        g_ExitRequested := true
        StopPlayback()
    } else
        ExitApp()
}

; ---------------------------------------------------------------------------
; Timing
; ---------------------------------------------------------------------------
QPC() {
    static freq := 0
    t := 0
    if !freq
        DllCall("QueryPerformanceFrequency", "Int64*", &freq)
    DllCall("QueryPerformanceCounter", "Int64*", &t)
    return t * 1000000 // freq
}

SleepUntilUS(target_us) {
    global g_Abort
    static timer := 0
    if !timer {
        try timer := DllCall("CreateWaitableTimerExW", "Ptr", 0, "Ptr", 0,
            "UInt", 0x2, "UInt", 0x1F0003, "Ptr")
        if !timer
            timer := DllCall("CreateWaitableTimerW", "Ptr", 0, "Int", 0, "Ptr", 0, "Ptr")
    }
    while !g_Abort {
        remaining := target_us - QPC()
        if (remaining <= 0)
            return true
        if (timer && remaining > 500) {
            wait_us := Min(remaining - 300, 2000)
            due := -wait_us * 10
            DllCall("SetWaitableTimer", "Ptr", timer, "Int64*", &due,
                "Int", 0, "Ptr", 0, "Ptr", 0, "Int", 0)
            DllCall("WaitForSingleObject", "Ptr", timer, "UInt", 2)
        }
    }
    return false
}

; ---------------------------------------------------------------------------
; Moves -> schedule
; ---------------------------------------------------------------------------
MoveClass(name) {
    return StrSplit(name, "#")[1]
}

MoveInstance(name) {
    parts := StrSplit(name, "#")
    return (parts.Length < 2 || !IsInteger(parts[2])) ? 1 : Integer(parts[2])
}

FindInOrder(order, name) {
    for i, n in order
        if (n == name)
            return i
    cls := MoveClass(name), idx := MoveInstance(name), seen := 0
    for i, n in order {
        if (MoveClass(n) == cls) {
            seen++
            if (seen = idx)
                return i
        }
    }
    if (idx = 1) {
        for i, n in order {
            if (n == cls)
                return i
        }
    }
    return 0
}

KeyForAction(action) {
    global KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT, KEY_PRIMARY, KEY_SECONDARY, KEY_F
    if (action == "N") {
        return KEY_UP
    }
    if (action == "S") {
        return KEY_DOWN
    }
    if (action == "W") {
        return KEY_LEFT
    }
    if (action == "E") {
        return KEY_RIGHT
    }
    if (action == "A" || SubStr(action, 1, 2) == "A@") {
        return KEY_PRIMARY
    }
    if (action == "T") {
        return KEY_SECONDARY
    }
    if (action == "F") {
        return KEY_F
    }
    if (StrLen(action) = 1 && IsDigit(action)) {
        return action
    }
    return ""
}

BuildSchedule(rec, speed := 1, delta_ticks := 0) {
    global KEY_SWITCH, TICK_US, DEFAULT_GAP_MS
    delta_us := delta_ticks * TICK_US
    moves := StrSplit(rec["moves"], " ")
    order := (rec["order"] != "") ? StrSplit(rec["order"], " ") : []
    ticks := (rec["ticks"] != "") ? StrSplit(rec["ticks"], ",") : []

    schedule := [], t_us := 0
    cur := (order.Length > 0) ? 1 : 0
    pending := 0
    for i, token in moves {
        if (token = "" || !InStr(token, ":"))
            continue
        parts := StrSplit(token, ":", , 2)
        actor := parts[1], action := parts[2]
        if (i <= ticks.Length && IsNumber(ticks[i]))
            t_us := Number(ticks[i]) * TICK_US
        else if (i > 1)
            t_us += DEFAULT_GAP_MS * 1000
        if (order.Length > 0) {
            tgt := FindInOrder(order, actor)
            if (tgt > 0 && tgt != cur) {
                n := order.Length
                pending += Mod(tgt - cur + n, n)
                cur := tgt
            }
        }
        key := KeyForAction(action)
        if (key != "") {
            schedule.Push({key: key, fire_at_us: t_us + delta_us, switches: pending})
            pending := 0
        }
    }
    if (pending > 0)
        schedule.Push({key: "", fire_at_us: t_us + delta_us, switches: pending})
    return schedule
}

; ---------------------------------------------------------------------------
; Input
; ---------------------------------------------------------------------------
SendTargetKey(keyName, direction) {
    global TARGET_WIN
    ControlSend("{" keyName " " direction "}", , TARGET_WIN)
}

ReleaseHeldKeys(held) {
    for keyName, _ in held
        try SendTargetKey(keyName, "up")
    held.Clear()
}

HasPendingRelease(releases, keyName) {
    for rel in releases
        if (rel.key = keyName)
            return true
    return false
}

; ---------------------------------------------------------------------------
; Playback
; ---------------------------------------------------------------------------
ReplayLevel(rec) {
    global g_Abort, g_Playing, g_Status, g_Fast, g_Slow, g_Shift2, g_Offset, g_Delta, g_ExitRequested
    global TARGET_WIN, RESET_KEY, PRESS_MS, KEY_SWITCH

    offset_us := (IsInteger(g_Offset.Value) ? Integer(g_Offset.Value) : 0) * 1000
    fast := (g_Fast.Value = 1)
    slow := (g_Slow.Value = 1)
    shiftHold := (g_Shift2.Value = 1)
    ; Clock factor: Alt = /4 wall-clock, Ctrl = *4 wall-clock. Only one can
    ; scale the scheduler at a time; Alt wins if both are set.
    speed := fast ? 4 : (slow ? 0.25 : 1)
    name := rec["name"]
    if (rec["moves"] = "") {
        g_Status.SetText("No 'moves' for " name)
        return
    }
    if !WinExist(TARGET_WIN) {
        g_Status.SetText("Game window not found: " TARGET_WIN)
        return
    }

    schedule := BuildSchedule(rec, speed,
        IsNumber(g_Delta.Value) ? Number(g_Delta.Value) : 0)
    if (schedule.Length = 0) {
        g_Status.SetText("Nothing to play for " name)
        return
    }

    WinActivate(TARGET_WIN)
    if !WinWaitActive(TARGET_WIN, , 2) {
        g_Status.SetText("Could not focus game window.")
        return
    }

    g_Abort := false
    g_Playing := true
    total := schedule.Length, idx := 0, aborted := false
    hold_us := PRESS_MS * 1000
    held := Map()
    DllCall("winmm\timeBeginPeriod", "UInt", 1)
    try {
        ; Hold modifier keys BEFORE the reset, and keep them down for the run.
        ; Shift changes the character's speed, not the clock; Alt/Ctrl scale time.
        if shiftHold {
            SendTargetKey("Shift", "down")
            held["Shift"] := true
        }
        if slow && !fast {
            SendTargetKey("Ctrl", "down")
            held["Ctrl"] := true
        }
        if fast {
            SendTargetKey("Alt", "down")
            held["Alt"] := true
        }
        if shiftHold || (slow && !fast) || fast
            Sleep(50)

        g_Status.SetText("Reset (" RESET_KEY ")...")
        SendTargetKey(RESET_KEY, "down")
        held[RESET_KEY] := true
        t0 := QPC()
        releases := [{key: RESET_KEY, at: t0 + hold_us}]
        for ev in schedule {
            if g_Abort {
                aborted := true
                break
            }
            deadline := t0 + Round((offset_us + ev.fire_at_us) / speed)
            ; Release any keys whose hold expires before this event.
            while (releases.Length > 0) {
                earliest := 1
                for ri, rel in releases
                    if (rel.at < releases[earliest].at)
                        earliest := ri
                if (releases[earliest].at > deadline)
                    break
                rel := releases.RemoveAt(earliest)
                if !SleepUntilUS(rel.at) {
                    aborted := true
                    break
                }
                if !HasPendingRelease(releases, rel.key) {
                    SendTargetKey(rel.key, "up")
                    held.Delete(rel.key)
                }
            }
            if aborted {
                break
            }
            if !SleepUntilUS(deadline) {
                aborted := true
                break
            }
            ; Character switches are tapped immediately before the action.
            Loop ev.switches {
                SendTargetKey(KEY_SWITCH, "down")
                SendTargetKey(KEY_SWITCH, "up")
            }
            if (ev.key != "") {
                keyName := (StrLen(ev.key) = 1) ? ev.key : SubStr(ev.key, 2, -1)
                SendTargetKey(keyName, "down")
                held[keyName] := true
                releases.Push({key: keyName, at: deadline + hold_us})
                idx++
                modeTag := (fast ? "  [4x]" : (slow ? "  [slow]" : ""))
                    . (shiftHold ? "  [2x]" : "")
                g_Status.SetText("Playing " name ": " idx "/" total modeTag)
            }
        }
        ; Release remaining keys in deadline order.
        while (releases.Length > 0 && !g_Abort) {
            earliest := 1
            for ri, rel in releases
                if (rel.at < releases[earliest].at)
                    earliest := ri
            rel := releases.RemoveAt(earliest)
            if !SleepUntilUS(rel.at) {
                aborted := true
                break
            }
            if !HasPendingRelease(releases, rel.key) {
                SendTargetKey(rel.key, "up")
                held.Delete(rel.key)
            }
        }
    } finally {
        ReleaseHeldKeys(held)
        DllCall("winmm\timeEndPeriod", "UInt", 1)
    }

    g_Playing := false
    g_Abort := false
    modeTag := (fast ? "  [4x]" : (slow ? "  [slow]" : ""))
        . (shiftHold ? "  [2x]" : "")
    g_Status.SetText(aborted
        ? "Stopped after " idx "/" total " keys." modeTag
        : "Done: " name " (" total " keys)." modeTag)
    if g_ExitRequested
        ExitApp()
}

; ---------------------------------------------------------------------------
; Main
; ---------------------------------------------------------------------------
ProcessSetPriority(PROCESS_PRIORITY)
BuildGui()
RefreshList()

F5::RefreshList()
~Esc::StopPlayback()
#HotIf g_Playing
*r::StopPlayback()
#HotIf
^j::PlaySelected()
