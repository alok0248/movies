// ===== Lazy-load TMDB extras for TV series (trailers, cast, crew) =====
(function() {
    var mediaType = 'tv';
    var tmdbId = SERIES_TMDB_ID;
    if (!tmdbId) return;

    fetch('/ajax/tmdb-extra/?media_type=' + mediaType + '&tmdb_id=' + tmdbId)
    .then(function(r) { return r.json(); })
    .then(function(data) {
        // Trailers
        var trailersEl = document.getElementById('lazy-trailers');
        if (trailersEl && data.trailers && data.trailers.length > 0) {
            var html = '<div class="det-section"><div class="det-section-head"><i class="fas fa-play-circle"></i> Videos &amp; Trailers</div><div class="det-trailer-scroll">';
            data.trailers.forEach(function(t) {
                html += '<div class="det-trailer-card" onclick="openTrailer(\'' + t.key + '\')">';
                html += '<img src="https://img.youtube.com/vi/' + t.key + '/hqdefault.jpg" alt="' + (t.name||'') + '" loading="lazy">';
                html += '<div class="det-trailer-overlay"><svg viewBox="0 0 64 64" fill="none"><circle cx="32" cy="32" r="30" stroke="white" stroke-width="3" opacity=".9"/><polygon points="26,20 46,32 26,44" fill="white"/></svg></div>';
                html += '<div class="det-trailer-label">' + (t.name||'') + '</div></div>';
            });
            html += '</div></div>';
            trailersEl.innerHTML = html;
        }
        // Cast
        var castEl = document.getElementById('lazy-cast');
        if (castEl && data.cast && data.cast.length > 0) {
            var castHtml = '<div class="det-section"><div class="det-section-head"><i class="fas fa-users"></i> Cast</div><div class="det-person-scroll">';
            data.cast.forEach(function(c) {
                if (!c.profile_path) return;
                castHtml += '<div class="det-person-card">';
                castHtml += '<a href="/person/' + c.id + '/"><img class="det-person-img" src="https://image.tmdb.org/t/p/w185' + c.profile_path + '" alt="' + (c.name||'') + '" loading="lazy"></a>';
                castHtml += '<a href="/person/' + c.id + '/" style="text-decoration:none;color:inherit"><div class="det-person-name">' + (c.name||'') + '</div></a>';
                castHtml += '<div class="det-person-role">' + (c.character||'') + '</div></div>';
            });
            castHtml += '</div></div>';
            castEl.innerHTML = castHtml;
        }
        // Crew
        var crewEl = document.getElementById('lazy-crew');
        if (crewEl && data.crew && data.crew.length > 0) {
            var crewHtml = '<div class="det-section"><div class="det-section-head"><i class="fas fa-film"></i> Featured Crew</div><div class="det-person-scroll">';
            data.crew.forEach(function(c) {
                if (!c.profile_path) return;
                crewHtml += '<div class="det-person-card">';
                crewHtml += '<a href="/person/' + c.id + '/"><img class="det-person-img det-crew-img" src="https://image.tmdb.org/t/p/w185' + c.profile_path + '" alt="' + (c.name||'') + '" loading="lazy"></a>';
                crewHtml += '<a href="/person/' + c.id + '/" style="text-decoration:none;color:inherit"><div class="det-person-name">' + (c.name||'') + '</div></a>';
                crewHtml += '<div class="det-person-role">' + (c.job||'') + '</div></div>';
            });
            crewHtml += '</div></div>';
            crewEl.innerHTML = crewHtml;
        }
    })
    .catch(function() {});
})();
