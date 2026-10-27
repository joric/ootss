#Requires AutoHotkey v2.0
#SingleInstance Force

; ---------------------------------------------------------------------------
; Config
; ---------------------------------------------------------------------------
TARGET_WIN     := "ahk_exe sinking_star.exe"
RESET_KEY      := "r"
START_OFFSET_MS := 0   ; default of the UI field: reset key press -> level time 0 (game time)

; Name of the level to auto-select when the list loads ("" = select first).
; Matches exactly, or by substring; if not found, falls back to the first entry.
DEFAULT_LEVEL  := "vertical_promesst_v2"

; Start with the "4x speed" checkbox enabled (true) or disabled (false).
DEFAULT_4X     := true

; Keys (same as your working Solution Player script)
KEY_UP        := "{Up}"
KEY_DOWN      := "{Down}"
KEY_LEFT      := "{Left}"
KEY_RIGHT     := "{Right}"
KEY_PRIMARY   := "x"      ; move token "A" / "A@n"
KEY_SECONDARY := "v"      ; move token "T"
KEY_SWITCH    := "c"      ; cycle to next character
KEY_F         := "f"      ; move token "F"
; tick / wait / pause send no key at all; they just take up their timestamp

; Timing
TICK_US        := 1       ; microseconds per unit of frame_ticks (1 = values are already in us)
DEFAULT_GAP_MS := 40      ; only used if a move has no frame_ticks value
MIN_GAP_MS     := 16      ; never send two keys closer together than this
PRESS_MS       := 40      ; how long each key is held down

; Precision: raise this thread's priority so the busy-wait and SendEvent calls
; are not preempted mid-send. AHK itself recommends High for delay=0 sending.
PROCESS_PRIORITY := "High"

global g_Abort   := false
global g_Playing := false
global g_LB      := ""
global g_Fast    := ""     ; "4x speed" checkbox
global g_Offset  := ""     ; start offset edit box
global g_Status  := ""
global g_Recs    := []     ; array of Map(name, moves, order, ticks), same order as the ListBox
global g_File    := ""

; solutions_debug.json lives in the same folder as this script.
ResolveSolutionsPath() {
    return A_ScriptDir "\solutions_debug.json"
}

; ---------------------------------------------------------------------------
; JSON loading with plain InStr/SubStr only (NO regex on the file text).
; ---------------------------------------------------------------------------

; p points right after a key's closing quote. Reads  : "value"
; Returns the value and sets endp to the position after it; endp := 0 if this
; was not a  key: "string"  pair.
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
    while (q && SubStr(text, q - 1, 1) = "\")     ; skip escaped quotes
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

; Finds the next  "key": "value"  at or after `from` (and before `to` if to > 0).
; Returns the position of the key (0 if none); val/endp are set on success.
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
    g_File := ResolveSolutionsPath()
    if !FileExist(g_File)
        throw Error("Solutions file not found:`n" g_File)

    text := FileRead(g_File, "UTF-8")

    ; 1) locate every record by its "name"
    starts := []
    names := []
    nends := []
    pos := 1
    while (np := FindKey(text, "name", pos, 0, &nm, &ne)) {
        starts.Push(np)
        names.Push(nm)
        nends.Push(ne)
        pos := ne
    }

    if (names.Length = 0)
        throw Error("No records with a name found.`n`nFile: " g_File "`nSize: " StrLen(text) " chars`n`nStart of file:`n" SubStr(text, 1, 300))

    ; 2) inside each record's text, read moves / order / frame_ticks
    recs := []
    for i, rn in names {
        from := nends[i]
        stop := (i < names.Length) ? starts[i + 1] : StrLen(text) + 1
        ; search only inside this record's own text (never scan the whole file)
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
    global g_LB, g_Fast, g_Offset, g_Status
    global START_OFFSET_MS, DEFAULT_4X

    g := Gui("+Resize", "WikiTactics Player")
    g.SetFont("s10", "Segoe UI")
    g.MarginX := 10
    g.MarginY := 10

    g.Add("Text", , "Levels:")
    g_LB := g.Add("ListBox", "w420 h400")
    g_LB.OnEvent("DoubleClick", (*) => PlaySelected())

    g_Fast := g.Add("Checkbox", "xm" (DEFAULT_4X ? " Checked" : ""),
        "4x speed (holds Alt before reset and during play)")

    g.Add("Text", "xm y+8", "Start offset after reset (ms, game time):")
    g_Offset := g.Add("Edit", "x+8 w70 Number", START_OFFSET_MS)

    btnPlay   := g.Add("Button", "xm y+10 w100 Default", "Play")
    btnStop   := g.Add("Button", "w100 x+10", "Stop")
    btnReload := g.Add("Button", "w120 x+10", "Reload list")
    btnClose  := g.Add("Button", "w100 x+10", "Close")

    btnPlay.OnEvent("Click",   (*) => PlaySelected())
    btnStop.OnEvent("Click",   (*) => StopPlayback())
    btnReload.OnEvent("Click", (*) => RefreshList())
    btnClose.OnEvent("Click",  (*) => ExitApp())

    g_Status := g.Add("StatusBar", , "Ready.")
    g.OnEvent("Close",  (*) => ExitApp())
    g.OnEvent("Escape", (*) => ExitApp())

    g.Show("w560")
    return g
}

RefreshList() {
    global g_LB, g_Recs, g_Status, g_File, DEFAULT_LEVEL

    g_Status.SetText("Loading...")
    try {
        recs := LoadSolutions()
    } catch as e {
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

    ; pick the preferred level: exact match first, then substring, else first
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
    global g_Abort, g_Status
    g_Abort := true
    g_Status.SetText("Stopping...")
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
    while !g_Abort {
        now := QPC()
        if (now >= target_us)
            return true
        if (target_us - now > 2500)
            Sleep(1)            ; coarse wait; the last ~2.5 ms are spun for precision
    }
    return false
}

; ---------------------------------------------------------------------------
; Moves -> key schedule
; ---------------------------------------------------------------------------
MoveClass(name) {
    return StrSplit(name, "#")[1]
}

MoveInstance(name) {
    parts := StrSplit(name, "#")
    if (parts.Length < 2 || !IsInteger(parts[2]))
        return 1
    return Integer(parts[2])
}

; Position (1-based) of an actor in the C-cycle order, 0 if not in it.
; Falls back to class + instance number ("priestess#2"), and to bare class names.
FindInOrder(order, name) {
    for i, n in order {
        if (n == name)
            return i
    }
    cls := MoveClass(name)
    idx := MoveInstance(name)
    seen := 0
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

; Move action -> key to send ("" = no key, just let time pass)
KeyForAction(action) {
    global KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT, KEY_PRIMARY, KEY_SECONDARY, KEY_F
    if (action == "N")
        return KEY_UP
    if (action == "S")
        return KEY_DOWN
    if (action == "W")
        return KEY_LEFT
    if (action == "E")
        return KEY_RIGHT
    if (action == "A" || SubStr(action, 1, 2) == "A@")
        return KEY_PRIMARY
    if (action == "T")
        return KEY_SECONDARY
    if (action == "F")
        return KEY_F
    if (StrLen(action) = 1 && IsDigit(action))
        return action
    return ""         ; tick / wait / pause: no key, they only use up their timestamp
}

BuildSchedule(rec, speed := 1) {
    global KEY_SWITCH, TICK_US, DEFAULT_GAP_MS, MIN_GAP_MS

    moves := StrSplit(rec["moves"], " ")
    order := (rec["order"] != "") ? StrSplit(rec["order"], " ") : []
    ticks := (rec["ticks"] != "") ? StrSplit(rec["ticks"], ",") : []

    schedule := []
    t_us := 0
    last := -1000000000
    gap := MIN_GAP_MS * 1000 * speed         ; gap is in game time; real gap stays MIN_GAP_MS
    cur := (order.Length > 0) ? 1 : 0        ; first actor in the roster starts selected

    for i, token in moves {
        if (token = "" || !InStr(token, ":"))
            continue
        parts := StrSplit(token, ":", , 2)
        actor := parts[1]
        action := parts[2]

        ; frame_ticks[i] = absolute timestamp (us from start) of move i
        if (i <= ticks.Length && IsNumber(ticks[i]))
            t_us := Number(ticks[i]) * TICK_US
        else if (i > 1)
            t_us += DEFAULT_GAP_MS * 1000

        ; switch character with C presses (cycle order)
        if (order.Length > 0) {
            tgt := FindInOrder(order, actor)
            if (tgt > 0 && tgt != cur) {
                n := order.Length
                Loop Mod(tgt - cur + n, n) {
                    fire := Max(t_us, last + gap)
                    schedule.Push({key: KEY_SWITCH, fire_at_us: fire})
                    last := fire
                }
                cur := tgt
            }
        }

        key := KeyForAction(action)
        if (key != "") {
            fire := Max(t_us, last + gap)
            schedule.Push({key: key, fire_at_us: fire})
            last := fire
        }
    }
    return schedule
}

; ---------------------------------------------------------------------------
; Precision key sending
;
; SetKeyDelay's PressDuration is subject to the OS timer granularity (often
; 10-15 ms), so a nominal 40 ms hold can actually last 40-55 ms. To get a
; deterministic hold we send key-down, busy-wait with QPC for exactly
; PRESS_MS microseconds, then send key-up. Gap between keys is handled by
; the scheduler's fire_at_us, not by SetKeyDelay.
; ---------------------------------------------------------------------------
SendKeyRaw(k, hold_us, blind := false) {
    ; Normalise single characters to the {x} form so {Alt} / {Blind} parse right.
    keyStr := (StrLen(k) = 1) ? "{" k "}" : k
    prefix := blind ? "{Blind}" : ""
    SendEvent(prefix "{" (StrLen(k) = 1 ? k : SubStr(keyStr, 2, -1)) " down}")
    SleepUntilUS(QPC() + hold_us)
    SendEvent(prefix "{" (StrLen(k) = 1 ? k : SubStr(keyStr, 2, -1)) " up}")
}

; ---------------------------------------------------------------------------
; Playback (moves + order + frame_ticks)
; ---------------------------------------------------------------------------
ReplayLevel(rec) {
    global g_Abort, g_Playing, g_Status, g_Fast, g_Offset
    global TARGET_WIN, RESET_KEY, PRESS_MS

    offset_ms := IsInteger(g_Offset.Value) ? Integer(g_Offset.Value) : 0
    offset_us := offset_ms * 1000
    fast := (g_Fast.Value = 1)
    speed := fast ? 4 : 1                   ; holding Alt in the game = 4x
    name := rec["name"]
    if (rec["moves"] = "") {
        g_Status.SetText("No 'moves' for " name " (was the JSON made with --info?)")
        return
    }
    if !WinExist(TARGET_WIN) {
        g_Status.SetText("Game window not found: " TARGET_WIN)
        return
    }

    schedule := BuildSchedule(rec, speed)
    if (schedule.Length = 0) {
        g_Status.SetText("Nothing to play for " name)
        return
    }

    WinActivate(TARGET_WIN)
    if !WinWaitActive(TARGET_WIN, , 2) {
        g_Status.SetText("Could not focus game window.")
        return
    }

    ; No SetKeyDelay: SendKeyRaw drives the hold with QPC. Keep key delay at 0
    ; so SendEvent itself adds no extra pause after each down/up.
    SetKeyDelay(0, -1)

    g_Abort := false
    g_Playing := true

    total := schedule.Length
    idx := 0
    aborted := false
    hold_us := PRESS_MS * 1000
    DllCall("winmm\timeBeginPeriod", "UInt", 1)    ; 1 ms timer resolution
    try {
        ; 4x mode: hold Alt BEFORE the reset and keep it down for the whole run
        if fast {
            SendEvent("{Alt down}")
            Sleep(50)
        }

        ; The clock starts at the reset key press. Timestamp 0 of the level is
        ; offset_us (game time) later, because the reset itself takes time.
        g_Status.SetText("Reset (" RESET_KEY ")...")
        t0 := QPC()
        SendKeyRaw(RESET_KEY, hold_us, fast)
        for ev in schedule {
            if g_Abort {
                aborted := true
                break
            }
            ; schedule times are "game time"; real time is game time / speed
            if !SleepUntilUS(t0 + (offset_us + ev.fire_at_us) // speed) {
                aborted := true
                break
            }
            idx++
            SendKeyRaw(ev.key, hold_us, fast)
            g_Status.SetText("Playing " name ": " idx "/" total (fast ? "  [4x]" : ""))
        }
    } finally {
        if fast
            SendEvent("{Alt up}")            ; always release Alt, even on Stop/error
        DllCall("winmm\timeEndPeriod", "UInt", 1)
    }

    g_Playing := false
    g_Abort := false
    g_Status.SetText(aborted
        ? "Stopped after " idx "/" total " keys."
        : "Done: " name " (" total " keys).")
}

; ---------------------------------------------------------------------------
; Main
; ---------------------------------------------------------------------------
ProcessSetPriority(PROCESS_PRIORITY)
BuildGui()
RefreshList()

F5::RefreshList()
~Esc::StopPlayback()

; Ctrl+F plays the selected level (mirrors the Play button).
^f::PlaySelected()
