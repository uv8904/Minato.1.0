/* ==========================================================================
   Stream Mode · "Coming Soon" (upcoming releases + live countdown)
   Served from the bot's own web server (/static/coming_soon.js).

   * Reads its configuration from the <section data-*> attributes:
         data-api   → JSON endpoint   (default "/api/movies/upcoming")
         data-limit → cards to show   (server caps it at 24)
         data-bot   → BOT_USERNAME    (public @username, never the token)
         data-poll  → feed refresh    (seconds, 0 = off; default 0)
   * The countdown is the point of this section, so it is the one thing that
     does NOT depend on the network: every card carries the release day as an
     ISO timestamp and a single shared 1-second timer re-renders the numbers
     locally.  No polling is needed to watch a countdown fall, which is why
     data-poll defaults to off – the list itself only changes when the bot
     re-reads TMDB (COMING_SOON_REFRESH_HOURS, default 6h).
   * When a countdown reaches zero the card flips to "releases today" on its
     own, without a reload – the same transition the server would render.
   * Builds every node with DOM APIs and textContent – API data is never
     passed through innerHTML, and poster/deep-link URLs are re-validated
     here as well (the server already whitelists them; this is belt and
     braces against a tampered response).
   * Every card deep-links to https://t.me/<BOT_USERNAME>?start=movie_<MOVIE_ID>
     so the movie opens directly inside the Telegram bot.
   ========================================================================== */
(function () {
    "use strict";

    var root = document.getElementById("comingSoon");
    if (!root) {
        return;
    }

    var apiUrl = (root.getAttribute("data-api") || "/api/movies/upcoming").trim();
    var limit = clampInt(root.getAttribute("data-limit"), 1, 24, 12);
    var pollSeconds = clampInt(root.getAttribute("data-poll"), 0, 3600, 0);
    var fallbackBot = (root.getAttribute("data-bot") || "").replace(/^@/, "").trim();
    var grid = root.querySelector("[data-cs-grid]");
    var chipText = root.querySelector("[data-cs-chip-text]");
    var requestTimeout = 9000;

    var SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
    var SAFE_BOT = /^[A-Za-z0-9_]{4,32}$/;
    var SAFE_DEEPLINK = /^https:\/\/t\.me\/([A-Za-z0-9_]{4,32})\?start=movie_[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
    var inFlight = null;
    var lastSignature = "";
    var pollTimer = null;
    var tickTimer = null;
    var hasRenderedOnce = false;
    /* Cards that need a per-second repaint: {node, iso}. */
    var liveCards = [];
    var etag = "";

    /* ------------------------------ helpers ------------------------------ */
    function clampInt(value, min, max, fallback) {
        var parsed = parseInt(value, 10);
        if (isNaN(parsed)) {
            return fallback;
        }
        return Math.max(min, Math.min(max, parsed));
    }

    function safeText(value, maxLength) {
        var text = String(value === null || value === undefined ? "" : value);
        text = text.replace(/[\u0000-\u001f\u007f]/g, "").replace(/[<>]/g, "").replace(/\s+/g, " ").trim();
        if (maxLength && text.length > maxLength) {
            text = text.slice(0, maxLength - 1).trim() + "…";
        }
        return text;
    }

    function safeMovieId(value) {
        var id = safeText(value, 64);
        return SAFE_ID.test(id) ? id : "";
    }

    /* Never trust an artwork URL: same-origin (or root-relative) images only. */
    function safePoster(value) {
        var url = String(value || "").trim();
        if (!url) {
            return "";
        }
        if (url.charAt(0) === "/" && url.charAt(1) !== "/") {
            return url;
        }
        try {
            var parsed = new URL(url, window.location.href);
            if (parsed.origin === window.location.origin) {
                return parsed.pathname + parsed.search;
            }
        } catch (err) {
            /* fall through */
        }
        return "";
    }

    /* Only a t.me/<bot>?start=movie_<id> deep link is ever followed; anything
       else (a hostile API, a javascript: URL) makes the card fall back to the
       plain bot link built from the validated @username. */
    function safeDeeplink(value) {
        var url = String(value || "").trim();
        return SAFE_DEEPLINK.test(url) ? url : "";
    }

    function safeBot(value) {
        var bot = safeText(value, 32).replace(/^@/, "");
        return SAFE_BOT.test(bot) ? bot : "";
    }

    function pad(value) {
        var text = String(Math.max(0, value | 0));
        return text.length < 2 ? "0" + text : text;
    }

    function releaseMoment(iso) {
        var moment = new Date(String(iso || ""));
        return isNaN(moment.getTime()) ? null : moment;
    }

    /* The same breakdown coming_soon.countdown_parts() uses server side. */
    function partsUntil(iso) {
        var moment = releaseMoment(iso);
        if (!moment) {
            return null;
        }
        var total = Math.floor((moment.getTime() - Date.now()) / 1000);
        if (total < 0) {
            total = 0;
        }
        return {
            done: total <= 0,
            days: Math.floor(total / 86400),
            hours: Math.floor(total / 3600) % 24,
            minutes: Math.floor(total / 60) % 60,
            seconds: total % 60
        };
    }

    /* ------------------------------ states ------------------------------- */
    function clearGrid() {
        while (grid && grid.firstChild) {
            grid.removeChild(grid.firstChild);
        }
        liveCards = [];
    }

    function skeletons(count) {
        clearGrid();
        if (grid) {
            grid.setAttribute("aria-busy", "true");
        }
        for (var index = 0; index < count; index += 1) {
            var card = document.createElement("div");
            card.className = "cs-card cs-skeleton";
            var art = document.createElement("div");
            art.className = "cs-art cs-ph";
            var body = document.createElement("div");
            body.className = "cs-body";
            var line1 = document.createElement("div");
            line1.className = "cs-ph cs-line";
            var line2 = document.createElement("div");
            line2.className = "cs-ph cs-line cs-line-short";
            body.appendChild(line1);
            body.appendChild(line2);
            card.appendChild(art);
            card.appendChild(body);
            grid.appendChild(card);
        }
    }

    function message(text, isError, retryable) {
        clearGrid();
        if (grid) {
            grid.setAttribute("aria-busy", "false");
        }
        var wrap = document.createElement("div");
        wrap.className = "cs-note" + (isError ? " cs-note-error" : "");
        var para = document.createElement("p");
        para.textContent = safeText(text, 220) || "Nothing here yet.";
        wrap.appendChild(para);
        if (retryable) {
            var button = document.createElement("button");
            button.type = "button";
            button.className = "cs-retry";
            button.textContent = "Try again";
            button.addEventListener("click", function () {
                load();
            });
            wrap.appendChild(button);
        }
        grid.appendChild(wrap);
    }

    /* ------------------------------ rendering ---------------------------- */
    function countdownNode() {
        var clock = document.createElement("div");
        clock.className = "cs-clock";
        ["days", "hrs", "min", "sec"].forEach(function (label, index) {
            var unit = document.createElement("span");
            unit.className = "cs-unit";
            var value = document.createElement("b");
            value.setAttribute("data-cs-part", index === 0 ? "days" : index === 1 ? "hours" : index === 2 ? "minutes" : "seconds");
            value.textContent = "00";
            var caption = document.createElement("i");
            caption.textContent = label;
            unit.appendChild(value);
            unit.appendChild(caption);
            clock.appendChild(unit);
        });
        return clock;
    }

    function paintCountdown(entry) {
        var parts = partsUntil(entry.iso);
        if (!parts) {
            return;
        }
        if (parts.done) {
            /* Zero reached: mirror what the server would render instead of
               freezing on 00:00:00. */
            entry.card.classList.add("cs-released");
            var clock = entry.card.querySelector(".cs-clock");
            if (clock && clock.parentNode) {
                var note = document.createElement("p");
                note.className = "cs-now";
                note.textContent = "Releases today — check back for the upload";
                clock.parentNode.replaceChild(note, clock);
            }
            entry.remove = true;
            return;
        }
        ["days", "hours", "minutes", "seconds"].forEach(function (name) {
            var node = entry.card.querySelector('[data-cs-part="' + name + '"]');
            if (node) {
                node.textContent = pad(parts[name]);
            }
        });
    }

    function buildCard(movie) {
        var card = document.createElement("article");
        card.className = "cs-card";
        if (movie.state === "soon") {
            card.classList.add("cs-soon");
        }
        if (movie.state === "released") {
            card.classList.add("cs-released");
        }

        /* ---- artwork ------------------------------------------------- */
        var figure = document.createElement("div");
        figure.className = "cs-art";
        var poster = safePoster(movie.poster);
        if (poster) {
            var image = document.createElement("img");
            image.loading = "lazy";
            image.decoding = "async";
            image.alt = "";
            image.src = poster;
            image.addEventListener("error", function () {
                figure.classList.add("cs-noart");
                if (image.parentNode) {
                    image.parentNode.removeChild(image);
                }
            });
            figure.appendChild(image);
        } else {
            figure.classList.add("cs-noart");
        }

        var chip = document.createElement("span");
        chip.className = "cs-chip";
        if (movie.state === "released") {
            chip.textContent = "released";
        } else if (movie.state === "soon") {
            chip.textContent = "releasing soon";
        } else {
            chip.textContent = movie.release_label || "coming soon";
        }
        figure.appendChild(chip);
        card.appendChild(figure);

        /* ---- text ---------------------------------------------------- */
        var body = document.createElement("div");
        body.className = "cs-body";

        var title = document.createElement("h3");
        title.className = "cs-name";
        title.textContent = safeText(movie.title, 90) || "Untitled";
        body.appendChild(title);

        var meta = document.createElement("p");
        meta.className = "cs-meta";
        var bits = [];
        if (movie.year) {
            bits.push(safeText(movie.year, 8));
        }
        if (movie.countdown) {
            bits.push(safeText(movie.countdown, 40));
        }
        meta.textContent = bits.join(" · ");
        body.appendChild(meta);

        /* ---- the countdown ------------------------------------------- */
        if (movie.state === "released") {
            var note = document.createElement("p");
            note.className = "cs-now";
            note.textContent = "Releases today — check back for the upload";
            body.appendChild(note);
        } else if (movie.release_date) {
            body.appendChild(countdownNode());
        }

        if (movie.waiting > 0) {
            var waiting = document.createElement("p");
            waiting.className = "cs-waiting";
            waiting.textContent = "🔥 " + safeText(movie.waiting, 8) + " waiting";
            body.appendChild(waiting);
        }

        var deeplink = safeDeeplink(movie.deeplink);
        var bot = safeBot(fallbackBot);
        var action = document.createElement("a");
        action.className = "cs-open";
        action.rel = "noopener";
        if (deeplink) {
            action.href = deeplink;
            action.target = "_blank";
        } else if (bot) {
            action.href = "https://t.me/" + bot;
            action.target = "_blank";
        } else {
            /* No validated destination at all: a button that goes nowhere is
               more honest than one that navigates somewhere unexpected. */
            action.href = "#";
        }
        action.textContent = movie.state === "released" ? "Open in Telegram" : "Notify me in bot";
        body.appendChild(action);

        card.appendChild(body);

        if (movie.release_date && movie.state !== "released") {
            liveCards.push({card: card, iso: movie.release_date, remove: false});
        }
        return card;
    }

    function render(movies) {
        clearGrid();
        if (grid) {
            grid.setAttribute("aria-busy", "false");
        }
        var usable = (movies || []).filter(function (movie) {
            return movie && safeMovieId(movie.id) && safeText(movie.title, 90);
        });
        if (!usable.length) {
            message("No upcoming releases yet — the list refreshes on its own.", false, false);
            return;
        }
        var fragment = document.createDocumentFragment();
        usable.slice(0, limit).forEach(function (movie) {
            fragment.appendChild(buildCard(movie));
        });
        grid.appendChild(fragment);
        /* Paint immediately so the first frame is not a row of zeros. */
        liveCards.forEach(paintCountdown);
        liveCards = liveCards.filter(function (entry) {
            return !entry.remove;
        });
    }

    /* One shared timer for the whole rail: 60 cards cost one interval. */
    function startTicking() {
        if (tickTimer) {
            return;
        }
        tickTimer = setInterval(function () {
            if (!liveCards.length) {
                return;
            }
            liveCards.forEach(paintCountdown);
            liveCards = liveCards.filter(function (entry) {
                return !entry.remove;
            });
        }, 1000);
    }

    /* ------------------------------ loading ------------------------------ */
    function load() {
        if (inFlight) {
            return inFlight;
        }
        if (!hasRenderedOnce) {
            skeletons(Math.min(limit, 6));
        }

        var controller = typeof AbortController === "function" ? new AbortController() : null;
        var timeout = controller ? setTimeout(function () {
            controller.abort();
        }, requestTimeout) : null;

        var headers = {};
        if (etag) {
            headers["If-None-Match"] = etag;
        }

        inFlight = fetch(apiUrl, {
            headers: headers,
            cache: "no-store",
            credentials: "same-origin",
            signal: controller ? controller.signal : undefined
        }).then(function (response) {
            if (response.status === 304) {
                /* Unchanged list – the countdown keeps ticking on its own. */
                hasRenderedOnce = true;
                return null;
            }
            if (!response.ok) {
                throw new Error("HTTP " + response.status);
            }
            etag = response.headers.get("ETag") || "";
            return response.json();
        }).then(function (payload) {
            if (!payload) {
                return;
            }
            var signature = JSON.stringify(payload.movies || []);
            if (signature !== lastSignature || !hasRenderedOnce) {
                lastSignature = signature;
                /* The @username in the payload wins over data-bot, so the
                   deep links follow whichever bot served the page. */
                var served = safeBot(payload.bot_username);
                if (served) {
                    fallbackBot = served;
                }
                render(payload.movies || []);
            }
            hasRenderedOnce = true;
            if (chipText) {
                var count = (payload.movies || []).length;
                chipText.textContent = count ? count + " releases tracked" : "checking soon";
            }
            startTicking();
        }).catch(function () {
            if (!hasRenderedOnce) {
                message("Could not load the upcoming releases.", true, true);
            }
        }).then(function () {
            if (timeout) {
                clearTimeout(timeout);
            }
            inFlight = null;
        });

        return inFlight;
    }

    function startPolling() {
        if (!pollSeconds || pollTimer) {
            return;
        }
        pollTimer = setInterval(function () {
            if (document.visibilityState === "visible") {
                load();
            }
        }, Math.max(15, pollSeconds) * 1000);
    }

    skeletons(Math.min(limit, 6));
    load();
    startPolling();
})();
