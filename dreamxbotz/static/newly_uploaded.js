/* ==========================================================================
   Stream Mode · "Newly Uploaded Movies"
   Served from the bot's own web server (/static/newly_uploaded.js).

   * Reads its configuration from the <section data-*> attributes:
         data-api   → JSON endpoint   (default "/api/movies/new")
         data-limit → cards to show   (server caps it at 20)
         data-bot   → BOT_USERNAME    (public @username, never the token)
         data-poll  → live refresh    (seconds, 0 = off; default 60)
   * Renders loading (skeletons) / empty / error (+ retry) states.
   * The newest upload additionally gets a Prime-Video-style **spotlight**
     banner (16:9 backdrop, poster card, title, badges, deep link) above the
     rail – so a movie uploaded to the bot a minute ago is the first thing a
     visitor sees.
   * Live refresh: while the tab is visible the feed is re-checked every
     data-poll seconds (cheap: the API answers 304 through its ETag when
     nothing changed).  A brand-new upload slides into the spotlight and the
     rail without a page reload.
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
    var pollSeconds = clampInt(root.getAttribute("data-poll"), 0, 3600, 60);
    var fallbackBot = (root.getAttribute("data-bot") || "").replace(/^@/, "").trim();
    var grid = root.querySelector("[data-nu-grid]");
    var spotlight = root.querySelector("[data-nu-spotlight]");
    var chipText = root.querySelector("[data-nu-chip-text]");
    var requestTimeout = 9000;
    var refreshAfter = 5 * 60 * 1000;
    var minPoll = 15;

    var SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
    var SAFE_BOT = /^[A-Za-z0-9_]{4,32}$/;
    var SAFE_DEEPLINK = /^https:\/\/t\.me\/([A-Za-z0-9_]{4,32})\?start=movie_[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
    var lastLoadedAt = 0;
    var inFlight = null;
    var lastSignature = "";
    var lastNewestId = "";
    var pollTimer = null;
    var chipTimer = null;
    var hasRenderedOnce = false;

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

    function playIcon() {
        var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("fill", "currentColor");
        svg.setAttribute("aria-hidden", "true");
        var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", "M8 5.5v13a1 1 0 0 0 1.5.86l11-6.5a1 1 0 0 0 0-1.72l-11-6.5A1 1 0 0 0 8 5.5Z");
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

    function isFresh(added) {
        var label = safeText(added, 32);
        return !!label && /^(just now|min ago|h ago|yesterday)/.test(label.replace(/^\d+\s/, ""));
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
        hideSpotlight();
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

    function setChip(mode, extra) {
        if (!chipText) {
            return;
        }
        var labels = {
            loading: "loading…",
            error: "offline",
            empty: "waiting for uploads",
            live: "live from telegram",
            stale: "up to date",
            fresh: "new upload"
        };
        var text = labels[mode] || labels.live;
        if (extra) {
            text += " · " + safeText(extra, 28);
        }
        chipText.textContent = text;
        root.classList.toggle("nu-section--fresh", mode === "fresh");
        if (chipTimer) {
            window.clearTimeout(chipTimer);
            chipTimer = null;
        }
        if (mode === "fresh") {
            chipTimer = window.setTimeout(function () {
                setChip("live");
            }, 8000);
        }
    }

    /* ------------------------------- cards -------------------------------- */
    function posterImage(movie, title, width, srcsetWidths, sizes) {
        var poster = safePoster(movie.poster);
        var image = document.createElement("img");
        // Explicit attributes (not just IDL properties) so every browser and
        // any HTML sanitizer/validator sees the lazy-loading contract.
        image.setAttribute("loading", "lazy");
        image.setAttribute("decoding", "async");
        image.setAttribute("width", "300");
        image.setAttribute("height", "450");
        image.alt = title + " poster";
        if (poster) {
            image.src = withWidth(poster, width);
            image.srcset = srcsetWidths
                .map(function (w) {
                    return withWidth(poster, w) + " " + w + "w";
                })
                .join(", ");
            image.sizes = sizes;
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
        return image;
    }

    function buildCard(movie, isNewest) {
        var id = safeMovieId(movie.id);
        var title = safeText(movie.title, 120) || "Untitled";
        var deeplink = safeDeeplink(movie.deeplink, id);

        var card = document.createElement("a");
        card.className = "nu-card" + (isNewest ? " nu-card--newest" : "");
        card.href = deeplink || "https://t.me/";
        card.target = "_blank";
        card.rel = "noopener noreferrer";
        card.title = "Open " + title + " in Telegram";
        card.setAttribute("aria-label", title + " — open in Telegram");
        if (id) {
            card.setAttribute("data-movie-id", id);
        }

        var art = element("span", "nu-art");
        art.appendChild(posterImage(movie, title, 320, [200, 480], "(max-width: 640px) 44vw, 200px"));
        art.appendChild(element("span", "nu-shade"));

        var quality = safeText(movie.quality || movie.quality_label, 24);
        if (quality) {
            art.appendChild(element("span", "nu-badge", quality));
        }
        var added = safeText(movie.added, 32);
        if (isNewest || isFresh(added)) {
            art.appendChild(element("span", "nu-new", added === "just now" || isNewest ? "new" : added));
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

    /* ----------------------------- spotlight ------------------------------ */
    /* Prime-Video-style banner for the newest upload: wide artwork behind a
       poster card, title, badges and the same Telegram deep link. */
    function hideSpotlight() {
        if (!spotlight) {
            return;
        }
        while (spotlight.firstChild) {
            spotlight.removeChild(spotlight.firstChild);
        }
        spotlight.hidden = true;
        spotlight.classList.remove("nu-spot--fresh");
    }

    function buildSpotlight(movie, fresh) {
        var id = safeMovieId(movie.id);
        var title = safeText(movie.title, 120) || "Untitled";
        var deeplink = safeDeeplink(movie.deeplink, id);
        var poster = safePoster(movie.poster);
        var backdrop = safePoster(movie.backdrop) || poster;
        var added = safeText(movie.added, 32);
        var year = movie.year ? String(parseInt(movie.year, 10) || "") : "";
        var qualityLabel = safeText(movie.quality_label || movie.quality, 40);
        var badge = safeText(movie.quality || movie.quality_label, 24);

        var box = element("article", "nu-spot" + (fresh ? " nu-spot--fresh" : ""));
        box.setAttribute("aria-label", "Just added: " + title);
        if (id) {
            box.setAttribute("data-movie-id", id);
        }

        /* --- wide artwork ------------------------------------------------ */
        var bg = element("span", "nu-spot-bg");
        bg.setAttribute("aria-hidden", "true");
        if (backdrop) {
            var wide = document.createElement("img");
            wide.className = "nu-spot-backdrop";
            wide.alt = "";
            wide.setAttribute("decoding", "async");
            wide.src = withWidth(backdrop, 1280);
            wide.srcset = withWidth(backdrop, 720) + " 720w, " + withWidth(backdrop, 1280) + " 1280w";
            wide.sizes = "100vw";
            wide.addEventListener("load", function () {
                // A portrait image means "no 16:9 backdrop known yet" – turn the
                // poster into an ambient blurred background instead of cropping it.
                if (wide.naturalHeight > wide.naturalWidth) {
                    wide.classList.add("nu-spot-backdrop--poster");
                }
                wide.classList.add("nu-on");
            });
            wide.addEventListener("error", function () {
                if (poster && wide.getAttribute("data-fallback") !== "1") {
                    wide.setAttribute("data-fallback", "1");
                    wide.removeAttribute("srcset");
                    wide.removeAttribute("sizes");
                    wide.classList.add("nu-spot-backdrop--poster");
                    wide.src = withWidth(poster, 800);
                    return;
                }
                wide.classList.add("nu-off");
            });
            bg.appendChild(wide);
        }
        bg.appendChild(element("span", "nu-spot-shade"));
        box.appendChild(bg);

        /* --- content ----------------------------------------------------- */
        var inner = element("div", "nu-spot-inner");

        var posterCard = document.createElement("a");
        posterCard.className = "nu-spot-poster";
        posterCard.href = deeplink || "https://t.me/";
        posterCard.target = "_blank";
        posterCard.rel = "noopener noreferrer";
        posterCard.setAttribute("aria-label", "Open " + title + " in Telegram");
        var posterImg = posterImage(movie, title, 480, [320, 480, 640], "(max-width: 640px) 34vw, 220px");
        posterImg.setAttribute("loading", "eager");
        posterCard.appendChild(posterImg);
        if (badge) {
            posterCard.appendChild(element("span", "nu-badge", badge));
        }
        posterCard.appendChild(element("span", "nu-spot-ribbon", "new"));
        inner.appendChild(posterCard);

        var info = element("div", "nu-spot-info");

        var kicker = element("p", "nu-spot-kicker");
        kicker.appendChild(element("i", null));
        kicker.appendChild(element("span", null, "Just added"));
        if (added) {
            kicker.appendChild(element("span", "nu-spot-sep", "·"));
            kicker.appendChild(element("span", "nu-spot-added", added));
        }
        info.appendChild(kicker);

        info.appendChild(element("h3", "nu-spot-title", title));

        var meta = element("div", "nu-spot-meta");
        if (year) {
            meta.appendChild(element("span", "nu-spot-chip", year));
        }
        if (qualityLabel) {
            meta.appendChild(element("span", "nu-spot-chip is-quality", qualityLabel));
        }
        meta.appendChild(element("span", "nu-spot-chip", "Telegram"));
        info.appendChild(meta);

        info.appendChild(
            element(
                "p",
                "nu-spot-sub",
                "The newest upload on the bot. Tap the poster or the button and " +
                    title +
                    " opens straight away inside Telegram — no searching needed."
            )
        );

        var actions = element("div", "nu-spot-actions");
        var open = document.createElement("a");
        open.className = "nu-spot-btn nu-spot-btn-primary";
        open.href = deeplink || "https://t.me/";
        open.target = "_blank";
        open.rel = "noopener noreferrer";
        open.appendChild(playIcon());
        open.appendChild(element("span", null, "Open in Telegram"));
        actions.appendChild(open);

        var all = document.createElement("a");
        all.className = "nu-spot-btn nu-spot-btn-ghost";
        all.href = "#nuGrid";
        all.appendChild(icon(["M3 5h18v14H3z", "M8 5v14", "M16 5v14", "M3 12h18"]));
        all.appendChild(element("span", null, "All new uploads"));
        actions.appendChild(all);
        info.appendChild(actions);

        inner.appendChild(info);
        box.appendChild(inner);
        return box;
    }

    function renderSpotlight(movie, fresh) {
        if (!spotlight) {
            return;
        }
        if (!movie) {
            hideSpotlight();
            return;
        }
        var signature = movieSignature(movie);
        var current = spotlight.firstChild;
        if (current && current.getAttribute && current.getAttribute("data-nu-sig") === signature) {
            // Same movie, same artwork – keep the loaded banner, refresh the age.
            var age = current.querySelector(".nu-spot-added");
            if (age) {
                age.textContent = safeText(movie.added, 32);
            }
            spotlight.hidden = false;
            return;
        }
        hideSpotlight();
        var banner = buildSpotlight(movie, fresh);
        banner.setAttribute("data-nu-sig", signature);
        spotlight.appendChild(banner);
        spotlight.hidden = false;
    }

    /* ------------------------------- render ------------------------------- */
    /* What makes a card visually different: id, artwork version, upload
       time and quality.  The relative "added 2 min ago" label is refreshed
       in place instead, so a live re-check never rebuilds unchanged cards. */
    function movieSignature(movie) {
        return [
            safeMovieId(movie.id),
            safePoster(movie.poster),
            safeText(movie.uploaded_at, 32),
            safeText(movie.quality_label || movie.quality, 40)
        ].join("|");
    }

    function signatureOf(movies) {
        return movies.map(movieSignature).join("\n");
    }

    function refreshCardLabels(card, movie, isNewest) {
        var added = safeText(movie.added, 32);
        var sub = card.querySelector(".nu-card-sub");
        if (sub) {
            var parts = [];
            if (movie.year) {
                parts.push(String(parseInt(movie.year, 10) || ""));
            }
            if (added) {
                parts.push("added " + added);
            }
            sub.textContent = safeText(parts.filter(Boolean).join(" · "), 48);
        }
        var tag = card.querySelector(".nu-new");
        if (tag && !isNewest) {
            if (isFresh(added)) {
                tag.textContent = added === "just now" ? "new" : added;
            } else {
                tag.parentNode.removeChild(tag);
            }
        }
    }

    function render(movies) {
        var valid = [];
        movies.forEach(function (movie) {
            if (movie && safeMovieId(movie.id)) {
                valid.push(movie);
            }
        });
        if (!valid.length) {
            lastSignature = "";
            lastNewestId = "";
            hasRenderedOnce = true;
            showState(
                "empty",
                "No new movies uploaded yet",
                "Fresh releases appear here automatically as soon as they are uploaded to the Telegram bot."
            );
            return;
        }

        valid = valid.slice(0, limit);
        var signature = signatureOf(valid);
        var newestId = safeMovieId(valid[0].id);
        var fresh = hasRenderedOnce && !!lastNewestId && newestId !== lastNewestId;
        var showingCards = grid.getAttribute("aria-busy") === "false" && !grid.classList.contains("nu-grid--state");
        if (signature === lastSignature && showingCards) {
            // Nothing changed – keep the DOM (and the loaded images), just
            // refresh the "added … ago" labels.
            var cards = grid.querySelectorAll(".nu-card[data-nu-sig]");
            valid.forEach(function (movie, index) {
                var key = movieSignature(movie) + (index === 0 ? "|newest" : "");
                for (var position = 0; position < cards.length; position += 1) {
                    if (cards[position].getAttribute("data-nu-sig") === key) {
                        refreshCardLabels(cards[position], movie, index === 0);
                        break;
                    }
                }
            });
            renderSpotlight(valid[0], false);
            return;
        }

        // Reuse the cards that did not change (their posters stay loaded), so
        // a new upload simply slides in at the front.
        var existing = {};
        if (showingCards) {
            Array.prototype.forEach.call(grid.querySelectorAll(".nu-card[data-nu-sig]"), function (card) {
                existing[card.getAttribute("data-nu-sig")] = card;
            });
        }
        var fragment = document.createDocumentFragment();
        valid.forEach(function (movie, index) {
            var key = movieSignature(movie) + (index === 0 ? "|newest" : "");
            var card = existing[key];
            if (card) {
                delete existing[key];
                refreshCardLabels(card, movie, index === 0);
            } else {
                card = buildCard(movie, index === 0);
                card.setAttribute("data-nu-sig", key);
            }
            fragment.appendChild(card);
        });
        resetGrid();
        grid.setAttribute("aria-busy", "false");
        grid.appendChild(fragment);
        renderSpotlight(valid[0], fresh);

        if (fresh) {
            setChip("fresh", safeText(valid[0].title, 40));
        } else {
            setChip("live");
        }
        lastSignature = signature;
        lastNewestId = newestId;
        hasRenderedOnce = true;
    }

    /* -------------------------------- load -------------------------------- */
    /* force  → bypass every cache (retry button / manual refresh)
       silent → keep the current cards on screen while re-checking the feed
                (live refresh); the DOM is only touched when something changed */
    function load(force, silent) {
        if (inFlight) {
            return inFlight;
        }
        if (!silent) {
            showSkeletons();
        }
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
                cache: force ? "no-store" : silent ? "no-cache" : "default",
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
                if (silent && hasRenderedOnce) {
                    // A failed background check must not wipe a working rail.
                    return null;
                }
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

    /* ---------------------------- live refresh ---------------------------- */
    function schedulePoll() {
        if (pollTimer) {
            window.clearTimeout(pollTimer);
            pollTimer = null;
        }
        if (!pollSeconds) {
            return;
        }
        pollTimer = window.setTimeout(function () {
            pollTimer = null;
            if (!document.hidden) {
                load(false, true).then(schedulePoll, schedulePoll);
            } else {
                schedulePoll();
            }
        }, Math.max(minPoll, pollSeconds) * 1000);
    }

    /* Keep the rail fresh when the visitor comes back to the tab. */
    document.addEventListener("visibilitychange", function () {
        if (!document.hidden && lastLoadedAt && Date.now() - lastLoadedAt > refreshAfter) {
            load(false, true);
        }
    });

    load(false).then(schedulePoll, schedulePoll);

    /* Small public hook (debugging / manual refresh from the console).
       refresh()                → full reload with skeletons
       refresh({ silent: true }) → re-check quietly, only redraw on changes */
    window.MinatoNewlyUploaded = {
        refresh: function (options) {
            var silent = !!(options && options.silent);
            return load(true, silent);
        },
        section: root,
        spotlight: spotlight
    };
})();
