(function(){
  var root = document.getElementById("exec-status-root");
  var runId = root.getAttribute("data-run-id");
  var execId = root.getAttribute("data-exec-id");
  var token = root.getAttribute("data-token");
  var pageUrl = "/execute-status/" + runId + "/" + execId + "?token=" + encodeURIComponent(token);
  var jsonUrl = "/execute-status-json/" + runId + "/" + execId + "?token=" + encodeURIComponent(token);
  var cancelUrl = "/execute-status/" + runId + "/" + execId + "/cancel?token=" + encodeURIComponent(token);

  var fill = document.getElementById("progress-fill");
  var label = document.getElementById("progress-label");
  var etaRow = document.getElementById("eta-row");
  var queueNote = document.getElementById("queue-note");
  var caseList = document.getElementById("case-list");
  var staleHint = document.getElementById("stale-hint");
  var staleSecs = document.getElementById("stale-secs");
  var refreshBtn = document.getElementById("refresh-now-btn");
  var refreshFlash = document.getElementById("refresh-flash");
  var cancelBtn = document.getElementById("cancel-run-btn");
  var cancelHint = document.getElementById("cancel-hint");
  var cancelRequested = false;

  var pendingTimer = null;
  var lastRenderedAt = Date.now();
  var STATUS_TEXT = {NOT_STARTED: "Not started", IN_PROGRESS: "In progress", PASS: "PASS", FAIL: "FAIL", BLOCKED: "BLOCKED", CANCELLED: "Cancelled"};

  function formatEta(seconds) {
    if (seconds === null || seconds === undefined) return "Estimating time remaining…";
    var mins = Math.max(1, Math.round(seconds / 60));
    return "About " + mins + " minute" + (mins === 1 ? "" : "s") + " remaining, based on this run's own pace so far.";
  }

  function flashRefresh(msg) {
    refreshFlash.textContent = msg;
    refreshFlash.classList.add("show");
    setTimeout(function(){ refreshFlash.classList.remove("show"); }, 1500);
  }

  function render(s) {
    lastRenderedAt = Date.now();
    staleHint.hidden = true;
    var total = s.total || 0, completed = s.completed || 0;
    var pct = total ? Math.round((completed / total) * 100) : 0;
    fill.style.width = pct + "%";
    label.textContent = completed + " of " + total + " test cases done";

    if (s.state === "running" && completed < total) {
      etaRow.textContent = formatEta(s.eta_seconds);
      etaRow.hidden = false;
    } else {
      etaRow.hidden = true;
    }

    if (s.queue && s.queue.ahead_count > 0) {
      queueNote.textContent = s.queue.ahead_count + " other run" + (s.queue.ahead_count === 1 ? "" : "s") +
        " ahead of yours, sharing today's request capacity.";
      queueNote.hidden = false;
    } else {
      queueNote.hidden = true;
    }

    if (cancelBtn) {
      cancelBtn.hidden = !s.cancellable || cancelRequested;
    }

    caseList.innerHTML = "";
    (s.cases || []).forEach(function(c){
      var li = document.createElement("li");
      var status = c.status || "NOT_STARTED";
      if (status === "IN_PROGRESS") li.className = "is-current";

      var left = document.createElement("div");
      left.className = "case-left";
      var tcLine = document.createElement("div");
      tcLine.className = "case-tc";
      tcLine.textContent = c.tc_id;
      var titleLine = document.createElement("div");
      titleLine.className = "case-title";
      titleLine.textContent = c.title || "";
      left.appendChild(tcLine);
      left.appendChild(titleLine);

      var right = document.createElement("div");
      right.className = "case-right";
      if (status === "IN_PROGRESS" && c.step && c.max_steps) {
        // Deliberately does NOT show the raw step/max_steps numbers - a
        // client reading "step 17/60" could easily (and wrongly) read that
        // as "this test case has 60 steps," when max_steps is really an
        // internal safety cap on the automation agent's own tool-calling
        // loop, unrelated to the test case's actual complexity. A small
        // visual progress bar conveys "still working, making progress"
        // without implying a specific, misleading step count.
        var miniBar = document.createElement("span");
        miniBar.className = "step-progress-mini";
        var miniFill = document.createElement("span");
        miniFill.className = "step-progress-mini-fill";
        var pct = Math.min(100, Math.round((c.step / c.max_steps) * 100));
        miniFill.style.width = pct + "%";
        miniBar.appendChild(miniFill);
        right.appendChild(miniBar);
      }
      var pill = document.createElement("span");
      pill.className = "status-pill " + status;
      if (status === "IN_PROGRESS") {
        var sp = document.createElement("span");
        sp.className = "spinner sm";
        pill.appendChild(sp);
      }
      var pillText = document.createElement("span");
      pillText.textContent = STATUS_TEXT[status] || status;
      pill.appendChild(pillText);
      right.appendChild(pill);

      li.appendChild(left);
      li.appendChild(right);
      caseList.appendChild(li);
    });
  }

  function schedule(fn, ms) {
    if (pendingTimer) clearTimeout(pendingTimer);
    pendingTimer = setTimeout(fn, ms);
  }

  function poll(manual) {
    fetch(jsonUrl, {cache: "no-store"}).then(function(resp){
      if (!resp.ok) {
        if (manual) flashRefresh("Couldn't reach the server — try again");
        schedule(poll, 4000);
        return;
      }
      return resp.json();
    }).then(function(s){
      if (!s) return;
      render(s);
      if (manual) flashRefresh("Up to date");
      if (s.state === "done" || s.state === "error") {
        window.location.href = pageUrl;
        return;
      }
      schedule(poll, 2500);
    }).catch(function(){
      if (manual) flashRefresh("Couldn't reach the server — try again");
      schedule(poll, 4000);
    });
  }

  // Browsers throttle timers (setTimeout/setInterval) heavily in tabs the
  // person has switched away from - a normal battery-saving behaviour, not
  // a bug, but it makes this page look "stuck" if someone tabs away while a
  // batch runs. Polling immediately on return, and offering a manual
  // refresh that always gives visible feedback (even "nothing changed
  // since your last check"), means a stale view is never mistaken for a
  // broken one.
  document.addEventListener("visibilitychange", function(){
    if (document.visibilityState === "visible") poll(false);
  });
  refreshBtn.addEventListener("click", function(){ poll(true); });

  if (cancelBtn) {
    cancelBtn.addEventListener("click", function(){
      if (cancelRequested) return;
      if (!window.confirm("Stop this run? Test cases already finished keep their result; anything else will show as Cancelled.")) return;
      cancelRequested = true;
      cancelBtn.hidden = true;
      cancelHint.hidden = false;
      fetch(cancelUrl, {method: "POST", cache: "no-store"}).then(function(){
        poll(true);
      }).catch(function(){
        // Even if this particular request failed, the next regular poll
        // will show current state - never leave the client stuck with no
        // feedback at all.
        poll(true);
      });
    });
  }

  setInterval(function(){
    if (document.visibilityState === "visible") {
      var secs = Math.round((Date.now() - lastRenderedAt) / 1000);
      if (secs > 20) {
        staleSecs.textContent = secs;
        staleHint.hidden = false;
      }
    }
  }, 5000);

  poll(false);
})();
