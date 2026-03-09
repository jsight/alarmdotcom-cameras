/* Alarm.com Cameras - Add-on Web UI JavaScript */

(function () {
    "use strict";

    // Use relative URLs so Ingress path prefix is handled automatically
    var API = "api";

    // --- Utility ---

    async function apiFetch(path, options) {
        return fetch(API + "/" + path, options);
    }

    async function apiJson(path, options) {
        var resp = await apiFetch(path, options);
        return resp.json();
    }

    function showToast(message, type) {
        type = type || "info";
        var container = document.getElementById("toast-container");
        var toast = document.createElement("div");
        toast.className = "toast toast-" + type;
        toast.textContent = message;
        container.appendChild(toast);
        setTimeout(function () {
            toast.remove();
        }, 4000);
    }

    function formatTime(timestamp) {
        if (!timestamp) return "Never";
        var d = new Date(timestamp * 1000);
        return d.toLocaleString();
    }

    function formatDuration(seconds) {
        if (!seconds && seconds !== 0) return "--";
        if (seconds < 60) return seconds + "s";
        if (seconds < 3600) return Math.floor(seconds / 60) + "m " + (seconds % 60) + "s";
        var h = Math.floor(seconds / 3600);
        var m = Math.floor((seconds % 3600) / 60);
        return h + "h " + m + "m";
    }

    function escapeHtml(str) {
        var div = document.createElement("div");
        div.textContent = str || "";
        return div.innerHTML;
    }

    // --- Tab navigation ---

    var tabs = document.querySelectorAll(".tab");
    tabs.forEach(function (tab) {
        tab.addEventListener("click", function () {
            tabs.forEach(function (t) { t.classList.remove("active"); });
            document.querySelectorAll(".tab-content").forEach(function (c) {
                c.classList.remove("active");
            });
            tab.classList.add("active");
            var target = document.getElementById("tab-" + tab.dataset.tab);
            if (target) target.classList.add("active");
        });
    });

    function switchToTab(tabName) {
        tabs.forEach(function (t) { t.classList.remove("active"); });
        document.querySelectorAll(".tab-content").forEach(function (c) {
            c.classList.remove("active");
        });
        var tabBtn = document.querySelector('.tab[data-tab="' + tabName + '"]');
        if (tabBtn) tabBtn.classList.add("active");
        var target = document.getElementById("tab-" + tabName);
        if (target) target.classList.add("active");
    }

    // --- Auth badge ---

    var lastAuthStatus = null;

    function updateAuthBadge(status) {
        var badge = document.getElementById("auth-badge");
        if (status === "authenticated") {
            badge.textContent = "Connected";
            badge.className = "badge badge-ok";
        } else if (status === "captcha_required" || status === "2fa_required") {
            badge.textContent = "Action Needed";
            badge.className = "badge badge-warning";
            // Auto-switch to setup tab when challenge appears
            if (lastAuthStatus !== status) {
                switchToTab("setup");
            }
        } else if (status === "error") {
            badge.textContent = "Error";
            badge.className = "badge badge-error";
        } else if (status === "logging_in") {
            badge.textContent = "Logging in...";
            badge.className = "badge badge-unknown";
        } else {
            badge.textContent = "Not Connected";
            badge.className = "badge badge-unknown";
        }
        lastAuthStatus = status;
    }

    // --- Credentials form ---

    var credForm = document.getElementById("credentials-form");
    credForm.addEventListener("submit", async function (e) {
        e.preventDefault();
        var btn = document.getElementById("save-login-btn");
        btn.disabled = true;
        btn.textContent = "Saving...";

        try {
            var resp = await apiFetch("credentials", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    username: document.getElementById("username").value,
                    password: document.getElementById("password").value,
                }),
            });
            if (resp.ok) {
                showToast("Credentials saved. Logging in...", "success");
                // Clear password field for security
                document.getElementById("password").value = "";
                // Trigger login
                btn.textContent = "Logging in...";
                await apiFetch("auth/login", { method: "POST" });
                await pollStatus();
            } else {
                var data = await resp.json();
                showToast(data.error || "Failed to save credentials", "error");
            }
        } catch (err) {
            showToast("Connection error: " + err.message, "error");
        } finally {
            btn.disabled = false;
            btn.textContent = "Save & Login";
        }
    });

    // --- Challenge form ---

    var challengeForm = document.getElementById("challenge-form");
    var challengeBtn = challengeForm.querySelector("button[type='submit']");

    challengeForm.addEventListener("submit", async function (e) {
        e.preventDefault();
        challengeBtn.disabled = true;
        challengeBtn.textContent = "Submitting...";

        try {
            var resp = await apiFetch("auth/solve", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    solution: document.getElementById("challenge-solution").value,
                }),
            });
            var result = await resp.json();
            if (resp.ok) {
                showToast("Solution submitted", "success");
                document.getElementById("challenge-solution").value = "";
                if (result.status === "authenticated") {
                    showToast("Authenticated successfully!", "success");
                } else if (result.status === "captcha_required" || result.status === "2fa_required") {
                    showToast("Challenge not solved. Please try again.", "error");
                }
            } else {
                showToast(result.error || "Failed to submit solution", "error");
            }
            await pollStatus();
        } catch (err) {
            showToast("Connection error: " + err.message, "error");
        } finally {
            challengeBtn.disabled = false;
            challengeBtn.textContent = "Submit";
        }
    });

    // --- Clear browser profile ---

    document.getElementById("clear-profile-btn").addEventListener("click", async function () {
        if (!confirm("This will clear all saved cookies and session data. You will need to log in again (including 2FA). Continue?")) {
            return;
        }
        var btn = this;
        btn.disabled = true;
        btn.textContent = "Clearing...";
        try {
            var resp = await apiFetch("browser/clear-profile", { method: "POST" });
            var result = await resp.json();
            if (resp.ok) {
                showToast("Browser profile cleared. Please log in again.", "success");
                switchToTab("setup");
            } else {
                showToast(result.error || "Failed to clear profile", "error");
            }
            await pollStatus();
        } catch (err) {
            showToast("Connection error: " + err.message, "error");
        } finally {
            btn.disabled = false;
            btn.textContent = "Clear Browser Profile";
        }
    });

    // --- Resend 2FA code ---

    document.getElementById("resend-code-btn").addEventListener("click", async function () {
        var btn = this;
        btn.disabled = true;
        btn.textContent = "Sending...";
        try {
            var resp = await apiFetch("auth/resend", { method: "POST" });
            var result = await resp.json();
            if (resp.ok) {
                showToast("Resend requested. Check your phone/email.", "success");
            } else {
                showToast(result.error || "Failed to resend code", "error");
            }
            await pollStatus();
        } catch (err) {
            showToast("Connection error: " + err.message, "error");
        } finally {
            btn.disabled = false;
            btn.textContent = "Resend Code";
        }
    });

    // --- Camera list ---

    async function loadCameras() {
        try {
            var data = await apiJson("cameras");
            var grid = document.getElementById("cameras-grid");

            if (!data.cameras || data.cameras.length === 0) {
                grid.innerHTML = '<p class="placeholder">No cameras discovered yet. Configure credentials and log in first.</p>';
                return;
            }

            grid.innerHTML = "";
            data.cameras.forEach(function (cam) {
                var card = document.createElement("div");
                card.className = "camera-card";

                var snapshotInfo = "";
                if (cam.last_snapshot) {
                    snapshotInfo = formatTime(cam.last_snapshot);
                    if (cam.snapshot_width) {
                        snapshotInfo += " (" + cam.snapshot_width + "x" + cam.snapshot_height + ")";
                    }
                } else {
                    snapshotInfo = "No snapshot yet";
                }

                card.innerHTML =
                    '<img class="thumbnail" src="' + API + '/snapshot/' + encodeURIComponent(cam.id) + '?t=' + Date.now() + '" ' +
                    '  alt="' + escapeHtml(cam.name) + '" onerror="this.style.background=\'#333\';this.alt=\'No snapshot\'">' +
                    '<div class="camera-info">' +
                    '  <div class="camera-name">' + escapeHtml(cam.name) + '</div>' +
                    '  <div class="camera-meta">' +
                        escapeHtml(cam.model || "Unknown model") +
                        ' &middot; ' + escapeHtml(cam.status || "unknown") +
                    '</div>' +
                    '  <div class="camera-meta">' + escapeHtml(snapshotInfo) + '</div>' +
                    '</div>' +
                    '<div class="camera-actions">' +
                    '  <button class="btn-small" data-action="snapshot" data-camera-id="' + escapeHtml(cam.id) + '">Snapshot</button>' +
                    '  <button class="btn-small" data-action="liveview" data-camera-id="' + escapeHtml(cam.id) + '" data-camera-name="' + escapeHtml(cam.name) + '">Live View</button>' +
                    '</div>';
                grid.appendChild(card);
            });

            // Attach event listeners via delegation
            grid.querySelectorAll('[data-action="snapshot"]').forEach(function (btn) {
                btn.addEventListener("click", function () {
                    captureSnapshot(btn.dataset.cameraId);
                });
            });
            grid.querySelectorAll('[data-action="liveview"]').forEach(function (btn) {
                btn.addEventListener("click", function () {
                    startLiveView(btn.dataset.cameraId, btn.dataset.cameraName);
                });
            });
        } catch (err) {
            // Silently fail on polling
        }
    }

    async function captureSnapshot(cameraId) {
        showToast("Capturing snapshot...", "info");
        try {
            var resp = await apiFetch("snapshot/" + encodeURIComponent(cameraId) + "/capture", {
                method: "POST",
            });
            if (resp.ok) {
                showToast("Snapshot captured", "success");
                setTimeout(loadCameras, 500);
            } else {
                showToast("Failed to capture snapshot", "error");
            }
        } catch (err) {
            showToast("Connection error", "error");
        }
    }

    async function startLiveView(cameraId, cameraName) {
        showToast("Starting live view...", "info");
        try {
            var resp = await apiFetch("stream/" + encodeURIComponent(cameraId) + "/start", {
                method: "POST",
            });
            if (resp.ok) {
                var liveCard = document.getElementById("live-view-card");
                liveCard.style.display = "";
                document.getElementById("live-camera-name").textContent = cameraName;
                document.getElementById("live-stream").src =
                    API + "/stream/" + encodeURIComponent(cameraId) + "?t=" + Date.now();
                // Scroll to live view
                liveCard.scrollIntoView({ behavior: "smooth" });
            } else {
                var data = await resp.json();
                showToast(data.error || "Failed to start stream", "error");
            }
        } catch (err) {
            showToast("Connection error", "error");
        }
    }

    document.getElementById("stop-stream-btn").addEventListener("click", async function () {
        try {
            await apiFetch("stream/status").then(function (r) { return r.json(); }).then(async function (status) {
                if (status.camera_id) {
                    await apiFetch("stream/" + encodeURIComponent(status.camera_id) + "/stop", {
                        method: "POST",
                    });
                }
            });
        } catch (err) {
            // ignore
        }
        document.getElementById("live-view-card").style.display = "none";
        document.getElementById("live-stream").src = "";
        showToast("Live view stopped", "info");
    });

    document.getElementById("refresh-cameras-btn").addEventListener("click", async function () {
        var btn = this;
        btn.disabled = true;
        btn.textContent = "Refreshing...";
        showToast("Refreshing camera list...", "info");
        try {
            var resp = await apiFetch("cameras/refresh", { method: "POST" });
            var data = await resp.json();
            if (resp.ok) {
                showToast("Found " + data.cameras + " camera(s)", "success");
            } else {
                showToast("Refresh failed", "error");
            }
            await loadCameras();
        } catch (err) {
            showToast("Connection error", "error");
        } finally {
            btn.disabled = false;
            btn.textContent = "Refresh";
        }
    });

    // --- Status polling ---

    async function pollStatus() {
        try {
            // Health (comprehensive)
            var health = await apiJson("health");
            document.getElementById("status-version").textContent = health.version || "--";
            document.getElementById("status-auth").textContent = health.auth_status || "--";
            document.getElementById("status-cameras").textContent = health.cameras_count || "0";
            document.getElementById("status-stream").textContent =
                health.active_stream ? "Yes (" + health.active_stream + ")" : "No";
            document.getElementById("status-browser").textContent =
                health.browser_alive ? "Running" : "Stopped";
            document.getElementById("status-uptime").textContent =
                formatDuration(health.uptime_seconds);
            document.getElementById("status-last-auth").textContent =
                formatTime(health.last_auth_time);
            document.getElementById("status-last-snapshot").textContent =
                formatTime(health.last_snapshot_time);
            document.getElementById("status-cache-age").textContent =
                health.cameras_cache_age !== null
                    ? formatDuration(health.cameras_cache_age)
                    : "Not cached";

            // Auth status (updates badge)
            var auth = await apiJson("auth/status");
            updateAuthBadge(auth.status);

            // Show/hide challenge card
            var challengeCard = document.getElementById("challenge-card");
            if (auth.status === "captcha_required" || auth.status === "2fa_required") {
                challengeCard.style.display = "";
                var desc = document.getElementById("challenge-description");
                if (auth.status === "captcha_required") {
                    desc.textContent = "A CAPTCHA was detected. Please solve it below.";
                } else {
                    desc.textContent = "Two-factor authentication required. Enter your code below.";
                }
                // Load challenge screenshot
                var img = document.getElementById("challenge-image");
                img.src = API + "/auth/challenge?t=" + Date.now();
                img.style.display = "";
                // Show resend button only for 2FA
                document.getElementById("resend-code-btn").style.display =
                    auth.status === "2fa_required" ? "" : "none";
                // Focus the solution input
                document.getElementById("challenge-solution").focus();
            } else if (auth.status === "error") {
                // Show challenge card with error screenshot if available
                challengeCard.style.display = "";
                var desc = document.getElementById("challenge-description");
                desc.textContent = auth.message || "Login failed. See screenshot below for details.";
                var img = document.getElementById("challenge-image");
                img.src = API + "/auth/challenge?t=" + Date.now();
                img.style.display = "";
                document.getElementById("resend-code-btn").style.display = "none";
                // Hide the solution form for errors (not actionable)
                document.getElementById("challenge-form").style.display = "none";
            } else {
                challengeCard.style.display = "none";
                document.getElementById("challenge-form").style.display = "";
            }

            // Credential status
            var creds = await apiJson("credentials/status");
            var credStatus = document.getElementById("cred-status");
            if (creds.configured) {
                credStatus.textContent = "Credentials saved for: " + (creds.username || "unknown");
                credStatus.className = "cred-status-ok";
            } else {
                credStatus.textContent = "No credentials configured. Enter your alarm.com login below.";
                credStatus.className = "cred-status-none";
            }

            // Settings display
            document.getElementById("setting-snapshot-interval").textContent =
                (health.snapshot_interval || "--") + " minutes";
            document.getElementById("setting-stream-fps").textContent =
                (health.stream_fps || "--") + " fps";
            document.getElementById("setting-stream-timeout").textContent =
                (health.stream_timeout || "--") + " minutes";
            document.getElementById("setting-jpeg-quality").textContent =
                (health.jpeg_quality || "--") + "%";
            document.getElementById("setting-trusted-device-name").textContent =
                health.trusted_device_name || "--";

        } catch (err) {
            // Silently fail on polling
        }
    }

    // --- Debug screenshots ---

    function loadDebugScreenshots() {
        var img = document.getElementById("debug-login-screenshot");
        img.src = API + "/debug/screenshot/login_page?t=" + Date.now();
        img.onclick = function () { window.open(img.src, "_blank"); };

        var img2 = document.getElementById("debug-login-failed-screenshot");
        img2.src = API + "/debug/screenshot/login_failed?t=" + Date.now();
        img2.onclick = function () { window.open(img2.src, "_blank"); };

        var img3 = document.getElementById("debug-post-challenge-screenshot");
        img3.src = API + "/debug/screenshot/post_challenge?t=" + Date.now();
        img3.onclick = function () { window.open(img3.src, "_blank"); };
    }

    document.getElementById("refresh-debug-btn").addEventListener("click", function () {
        loadDebugScreenshots();
        showToast("Debug screenshots refreshed", "info");
    });

    // Initial load
    pollStatus();
    loadCameras();
    loadDebugScreenshots();

    // Poll status every 5 seconds, cameras every 30 seconds
    setInterval(pollStatus, 5000);
    setInterval(loadCameras, 30000);

})();
