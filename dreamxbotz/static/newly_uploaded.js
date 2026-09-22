/* ==========================================================================
   Stream Mode · "Newly Uploaded Movies"
   Served from the bot's own web server (/static/newly_uploaded.js).

   * Reads its configuration from the <section data-*> attributes:
         data-api   → JSON endpoint   (default "/api/movies/new")
         data-limit → cards to show   (server caps it at 20)
         data-bot   → BOT_USERNAME    (public @username, never the token)
   * Renders loading (skeletons) / empty / error (+ retry) states.
   * Builds every node with DOM APIs and textContent – API data is never
     passed through innerHTML, and poster/deep-link URLs are re-validated
     here as well.
   * Every card deep-links to https://t.me/<BOT_USERNAME>?start=movie_<MOVIE_ID>
     so the movie is opened directly inside the Telegram bot.
   ========================================================================== */
(function () {
    "use strict";

    var root = document.getElementById("newlyUploaded");
    if (!root) {
        return;
    }

    var apiUrl = (root.getAttribute("data-api") || "/api/movies/new").trim();
    var limit = clampInt(root.getAttribute("data-limit"), 1, 20, 20);
    var fallbackBot = (root.getAttribute("data-bot") || "").replace(/^@/, "").trim();
    var grid = root.querySelector("[data-nu-grid]");
    var chipText = root.querySelector("[data-nu-chip-text]");
    var requestTimeout = 9000;
    var refreshAfter = 5 * 60 * 1000;

    var SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
    var SAFE_BOT = /^[A-Za-z0-9_]{4,32}$/;
    var SAFE_DEEPLINK = /^https:\/\/t\.me\/([A-Za-z0-9_]{4,32})\?start=movie_[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
    var lastLoadedAt = 0;
    var inFlight = null;

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

    /* Never trust a poster URL: same-origin (or root-relative) images only. */
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
            return "";
        }
        return "";
    }

    function withWidth(url, width) {
        if (!url) {
            return "";
        }
        return url + (url.indexOf("?") === -1 ? "?" : "&") + "w=" + width;
    }

    /* The deep link must be t.me/<bot>?start=movie_<id> – otherwise we rebuild
       it from the public BOT_USERNAME delivered by the server. */
    function safeDeeplink(value, movieId) {
        var candidate = String(value || "").trim();
        if (SAFE_DEEPLINK.test(candidate)) {
            return candidate;
        }
        if (SAFE_BOT.test(fallbackBot) && SAFE_ID.test(movieId)) {
            return "https://t.me/" + fallbackBot + "?start=movie_" + movieId;
        }
        return "";
    }

    function escapeXml(value) {
        return String(value || "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&apos;");
    }

    /* Built-in placeholder: shown when a poster 404s or fails to decode. */
    function placeholderDataUri(title) {
        var label = escapeXml(safeText(title, 46));
        var svg =
            '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450" viewBox="0 0 300 450">' +
            '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">' +
            '<stop offset="0" stop-color="#f5c518"/><stop offset="1" stop-color="#ffdd7a"/>' +
            "</linearGradient>" +
            '<linearGradient id="b" x1="0" y1="0" x2="1" y2="1">' +
            '<stop offset="0" stop-color="#161a28"/><stop offset="1" stop-color="#080a11"/>' +
            "</linearGradient></defs>" +
            '<rect width="300" height="450" fill="url(#b)"/>' +
            '<circle cx="60" cy="70" r="96" fill="#f5c518" opacity="0.07"/>' +
            '<g transform="translate(96 150)" fill="none" stroke="url(#g)" stroke-width="3">' +
            '<rect x="0" y="0" width="108" height="96" rx="10"/>' +
            '<rect x="-13" y="-4" width="10" height="12" rx="3" fill="#f5c518" stroke="none"/>' +
            '<rect x="-13" y="26" width="10" height="12" rx="3" fill="#f5c518" stroke="none"/>' +
            '<rect x="-13" y="56" width="10" height="12" rx="3" fill="#f5c518" stroke="none"/>' +
            '<rect x="-13" y="86" width="10" height="12" rx="3" fill="#f5c518" stroke="none"/>' +
            '<rect x="111" y="-4" width="10" height="12" rx="3" fill="#f5c518" stroke="none"/>' +
            '<rect x="111" y="26" width="10" height="12" rx="3" fill="#f5c518" stroke="none"/>' +
            '<rect x="111" y="56" width="10" height="12" rx="3" fill="#f5c518" stroke="none"/>' +
            '<rect x="111" y="86" width="10" height="12" rx="3" fill="#f5c518" stroke="none"/>' +
            '<path d="M44 32 76 48 44 64Z" fill="#f5c518" stroke="none"/></g>' +
            '<text x="150" y="310" text-anchor="middle" font-family="Sora, Segoe UI, sans-serif" ' +
            'font-size="17" font-weight="600" fill="#f4f6fb">' + label + "</text>" +
            '<text x="150" y="336" text-anchor="middle" font-family="Inter, Segoe UI, sans-serif" ' +
            'font-size="11" letter-spacing="2" fill="#f5c518">MINATOVERSE</text></svg>';
        return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
    }

    function telegramIcon() {
        var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("fill", "currentColor");
        svg.setAttribute("aria-hidden", "true");
        var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute(
            "d",
            "M21.9 4.3 18.8 19c-.2 1-.9 1.3-1.7.8l-4.6-3.4-2.2 2.1c-.3.3-.5.5-1 .5l.3-4.6 8.4-7.6c.4-.3-.1-.5-.6-.2L7.1 12.8l-4.5-1.4c-1-.3-1-1 .2-1.4l17.6-6.8c.8-.3 1.5.2 1.5 1.1Z"
        );
        svg.appendChild(path);
        return svg;
    }

    function element(tag, className, text) {
        var node = document.createElement(tag);
        if (className) {
            node.className = className;
        }
        if (text !== undefined && text !== null) {
            node.textContent = text;
        }
        return node;
    }

    function icon(path) {
        var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("fill", "none");
        svg.setAttribute("stroke", "currentColor");
        svg.setAttribute("stroke-width", "2");
        svg.setAttribute("stroke-linecap", "round");
        svg.setAttribute("stroke-linejoin", "round");
        svg.setAttribute("aria-hidden", "true");
        path.forEach(function (d) {
            var node = document.createElementNS("http://www.w3.org/2000/svg", "path");
            node.setAttribute("d", d);
            svg.appendChild(node);
        });
        return svg;
    }

    /* ------------------------------- states ------------------------------- */
    function resetGrid() {
        while (grid.firstChild) {
            grid.removeChild(grid.firstChild);
        }
        grid.classList.remove("nu-grid--state");
        grid.setAttribute("aria-busy", "true");
    }

    function showSkeletons() {
        resetGrid();
        var count = Math.min(limit, 8);
        for (var index = 0; index < count; index += 1) {
            var card = element("div", "nu-skel");
            card.setAttribute("aria-hidden", "true");
            card.appendChild(element("span", "nu-skel-art"));
            var lines = element("span", "nu-skel-lines");
            lines.appendChild(element("span", "nu-skel-line"));
            lines.appendChild(element("span", "nu-skel-line short"));
            card.appendChild(lines);
            grid.appendChild(card);
        }
        setChip("loading");
    }

    function showState(kind, title, message, code) {
        resetGrid();
        grid.classList.add("nu-grid--state");
        grid.setAttribute("aria-busy", "false");
        var box = element("div", "nu-state");
        box.setAttribute("role", kind === "error" ? "alert" : "status");

        var badge = element("span", "nu-state-icon");
        badge.appendChild(
            kind === "error"
                ? icon(["M12 9v4", "M12 17h.01", "M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"])
                : icon(["M3 5h18v14H3z", "M8 5v14", "M16 5v14", "M3 12h18"])
        );
        box.appendChild(badge);
        box.appendChild(element("h3", "nu-state-title", title));
        box.appendChild(element("p", "nu-state-text", message));
        if (code) {
            box.appendChild(element("p", "nu-state-code", code));
        }
        if (kind === "error") {
            var retry = element("button", "nu-retry");
            retry.type = "button";
            retry.appendChild(icon(["M21 12a9 9 0 1 1-3-6.7", "M21 3v6h-6"]));
            retry.appendChild(element("span", null, "Try again"));
            retry.addEventListener("click", function () {
                load(true);
            });
            box.appendChild(retry);
        }
        grid.appendChild(box);
        setChip(kind === "error" ? "error" : "empty");
    }

    function setChip(mode) {
        if (!chipText) {
            return;
        }
        var labels = {
            loading: "loading…",
            error: "offline",
            empty: "waiting for uploads",
            live: "live from telegram",
            stale: "up to date"
        };
        chipText.textContent = labels[mode] || labels.live;
    }

    /* ------------------------------- cards -------------------------------- */
    function buildCard(movie) {
        var id = safeMovieId(movie.id);
        var title = safeText(movie.title, 120) || "Untitled";
        var deeplink = safeDeeplink(movie.deeplink, id);
        var poster = safePoster(movie.poster);

        var card = document.createElement("a");
        card.className = "nu-card";
        card.href = deeplink || "https://t.me/";
        card.target = "_blank";
        card.rel = "noopener noreferrer";
        card.title = "Open " + title + " in Telegram";
        card.setAttribute("aria-label", title + " — open in Telegram");

        var art = element("span", "nu-art");
        var image = document.createElement("img");
        // Explicit attributes (not just IDL properties) so every browser and
        // any HTML sanitizer/validator sees the lazy-loading contract.
        image.setAttribute("loading", "lazy");
        image.setAttribute("decoding", "async");
        image.setAttribute("width", "300");
        image.setAttribute("height", "450");
        image.alt = title + " poster";
        if (poster) {
            image.src = withWidth(poster, 320);
            image.srcset = withWidth(poster, 200) + " 200w, " + withWidth(poster, 480) + " 480w";
            image.sizes = "(max-width: 640px) 44vw, 200px";
        }
        image.addEventListener("load", function () {
            image.classList.add("nu-on");
        });
        image.addEventListener("error", function () {
            image.removeAttribute("srcset");
            image.removeAttribute("sizes");
            image.src = placeholderDataUri(title);
            image.classList.add("nu-on");
        });
        if (!poster) {
            image.src = placeholderDataUri(title);
        }
        art.appendChild(image);
        art.appendChild(element("span", "nu-shade"));

        var quality = safeText(movie.quality || movie.quality_label, 24);
        if (quality) {
            art.appendChild(element("span", "nu-badge", quality));
        }
        var added = safeText(movie.added, 32);
        if (added && /^(just now|min ago|h ago|yesterday)/.test(added.replace(/^\d+\s/, ""))) {
            art.appendChild(element("span", "nu-new", added === "just now" ? "new" : added));
        }

        var cta = element("span", "nu-cta");
        cta.appendChild(telegramIcon());
        cta.appendChild(element("span", null, "Open in Telegram"));
        art.appendChild(cta);

        var body = element("span", "nu-body");
        body.appendChild(element("span", "nu-card-title", title));
        var sub = [];
        if (movie.year) {
            sub.push(String(parseInt(movie.year, 10) || ""));
        }
        if (added) {
            sub.push("added " + added);
        }
        body.appendChild(
            element("span", "nu-card-sub", safeText(sub.filter(Boolean).join(" · "), 48))
        );
        art.appendChild(body);

        card.appendChild(art);
        return card;
    }

    function render(movies) {
        resetGrid();
        var valid = [];
        movies.forEach(function (movie) {
            if (movie && safeMovieId(movie.id)) {
                valid.push(movie);
            }
        });
        if (!valid.length) {
            showState(
                "empty",
                "No new movies uploaded yet",
                "Fresh releases appear here automatically as soon as they are uploaded to the Telegram bot."
            );
            return;
        }
        grid.setAttribute("aria-busy", "false");
        var fragment = document.createDocumentFragment();
        valid.slice(0, limit).forEach(function (movie) {
            fragment.appendChild(buildCard(movie));
        });
        grid.appendChild(fragment);
        setChip("live");
    }

    /* -------------------------------- load -------------------------------- */
    function load(force) {
        if (inFlight) {
            return inFlight;
        }
        showSkeletons();
        var url = apiUrl + (apiUrl.indexOf("?") === -1 ? "?" : "&") + "limit=" + limit;
        var controller = typeof AbortController !== "undefined" ? new AbortController() : null;
        var timer = window.setTimeout(function () {
            if (controller) {
                controller.abort();
            }
        }, requestTimeout);

        inFlight = window
            .fetch(url, {
                headers: { Accept: "application/json" },
                credentials: "omit",
                cache: force ? "no-store" : "default",
                signal: controller ? controller.signal : undefined
            })
            .then(function (response) {
                if (!response.ok) {
                    throw new Error("Server responded with HTTP " + response.status);
                }
                return response.json();
            })
            .then(function (data) {
                window.clearTimeout(timer);
                if (!data || data.ok === false || !Array.isArray(data.movies)) {
                    throw new Error("Unexpected API payload");
                }
                if (!fallbackBot && data.bot_username) {
                    fallbackBot = safeText(data.bot_username, 32).replace(/^@/, "");
                }
                lastLoadedAt = Date.now();
                render(data.movies);
                return data;
            })
            .catch(function (error) {
                window.clearTimeout(timer);
                var aborted = error && error.name === "AbortError";
                showState(
                    "error",
                    "Couldn't load new movies",
                    "The movie feed is unavailable right now. Check your connection and try again — the rest of the page keeps working.",
                    aborted ? "Request timed out" : safeText(error && error.message, 90)
                );
                return null;
            })
            .then(function (result) {
                inFlight = null;
                return result;
            });
        return inFlight;
    }

    /* Keep the rail fresh when the visitor comes back to the tab. */
    document.addEventListener("visibilitychange", function () {
        if (!document.hidden && lastLoadedAt && Date.now() - lastLoadedAt > refreshAfter) {
            load(false);
        }
    });

    load(false);

    /* Small public hook (debugging / manual refresh from the console). */
    window.MinatoNewlyUploaded = {
        refresh: function () {
            return load(true);
        },
        section: root
    };
})();
