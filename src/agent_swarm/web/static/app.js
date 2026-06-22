// P5-W28 WebSocket client — 实时事件流
(function () {
    "use strict";
    const statusEl = document.getElementById("ws-status");
    const eventListEl = document.getElementById("ws-event-list");
    if (!statusEl) return;

    function setStatus(connected) {
        const dot = statusEl.querySelector(".dot");
        const label = statusEl.querySelector(".label");
        if (dot) {
            dot.classList.toggle("on", connected);
            dot.classList.toggle("off", !connected);
        }
        if (label) {
            label.textContent = connected ? "live" : "disconnected";
        }
    }

    function appendEvent(rec) {
        if (!eventListEl) return;
        // 首条 "等待事件..." 删掉
        const placeholder = eventListEl.querySelector(".event-info");
        if (placeholder) placeholder.remove();
        const li = document.createElement("li");
        li.className = "event event-" + (rec.event_name || "unknown");
        const ts = new Date(rec.timestamp * 1000).toLocaleTimeString();
        li.innerHTML =
            '<span class="ts">' + ts + '</span> ' +
            '<span class="name">' + (rec.event_name || "?") + '</span> ' +
            '<span class="sid">' + (rec.session_id || "?").slice(0, 12) + '</span> ' +
            '<span class="payload">' + JSON.stringify(rec.payload || {}).slice(0, 200) + '</span>';
        eventListEl.insertBefore(li, eventListEl.firstChild);
        // 限 100 条
        while (eventListEl.children.length > 100) {
            eventListEl.removeChild(eventListEl.lastChild);
        }
    }

    function connect() {
        const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
        const url = proto + "//" + window.location.host + "/ws";
        const ws = new WebSocket(url);
        ws.onopen = function () { setStatus(true); };
        ws.onclose = function () {
            setStatus(false);
            // 5s 后重连
            setTimeout(connect, 5000);
        };
        ws.onerror = function () { setStatus(false); };
        ws.onmessage = function (evt) {
            try {
                const rec = JSON.parse(evt.data);
                if (rec.event_name === "_hello") return;  // 跳过 hello
                appendEvent(rec);
            } catch (e) {
                console.error("ws parse failed", e);
            }
        };
    }

    connect();
})();
