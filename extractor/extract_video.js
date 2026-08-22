const https = require("https");
const fs = require("fs");

function httpGet(url) {
  return new Promise((resolve, reject) => {
    https.get(url, {
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin": "https://player.videasy.to",
        "Referer": "https://player.videasy.to/"
      }
    }, (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => resolve(data));
    }).on("error", reject);
  });
}

// Extract the exact cipher/decrypt function from the player chunk
const chunkCode = fs.readFileSync("chunk_8351.js", "utf8");
const fnStart = chunkCode.indexOf("function(e,t,s){var a;let r=function(e){");
const fnEnd = chunkCode.indexOf("}(await (0,c.Wg)") + 1;
const decryptFnSource = chunkCode.substring(fnStart, fnEnd);

// Set up the closure dependencies
const f = [1116352408,1899447441,3049323471,3921009573,961987163,1508970993,2453635748,2870763221,3624381080,310598401,607225278,1426881987,1925078388,2162078206,2614888103,3248222580];
const h = [109,118,109,49];
const b = e => (e*(e+1)&1) === 0;
const I = e => (e*(e+1)&1) === 1;
function w(e) { return e>>>=0, e^=e>>>16, e=Math.imul(e,2246822507)>>>0, e^=e>>>13, e=Math.imul(e,3266489909)>>>0, (e^=e>>>16)>>>0; }
function v(e, t) { return (e>>>=0, 0==(t&=31)) ? e>>>0 : (e<<t|e>>>32-t)>>>0; }

// Create the decrypt function with dependencies in closure
const fnStr = '(function(f,h,b,I,w,v){return ' + decryptFnSource + '})';
const decryptFn = eval(fnStr)(f, h, b, I, w, v);

// Also extract the source-fetching functions from the chunk
// They call the decrypt function with: (apiResponse, seed, tmdbId)

(async () => {
  try {
    console.log("🎬 Fetching movie details for ID 969681...");
    const details = JSON.parse(await httpGet("https://db.speedracelight.com/3/movie/969681?append_to_response=external_ids&language=en"));
    const title = details.title;
    const year = new Date(details.release_date).getFullYear();
    const tmdbId = details.id;
    const imdbId = details.imdb_id || "";
    console.log(`   Movie: ${title} (${year})`);
    console.log(`   TMDB: ${tmdbId} | IMDB: ${imdbId}`);

    const seedResp = JSON.parse(await httpGet(`https://api.speedracelight.com/seed?mediaId=${tmdbId}`));
    const seed = seedResp.seed;
    console.log(`   Seed: ${seed}`);

    // Try multiple servers
    const servers = [
      { name: "CDN/Yoru (Original, may have 4K)", path: "cdn" },
      { name: "Breach/m4uhd (Original audio)", path: "m4uhd" },
      { name: "Neon (Original audio)", path: "vsrc" },
      { name: "HD Movie", path: "hdmovie" },
      { name: "Cypher/Downloader2", path: "downloader2" },
      { name: "Superflix", path: "superflix" },
      { name: "LaMovie", path: "lamovie" },
      { name: "Meine (German)", path: "meine" },
    ];

    for (const server of servers) {
      const url = `https://api.speedracelight.com/${server.path}/sources-with-title?title=${encodeURIComponent(title)}&mediaType=movie&year=${year}&tmdbId=${tmdbId}&imdbId=${imdbId}&enc=2&seed=${encodeURIComponent(seed)}`;
      process.stdout.write(`\n📡 Trying ${server.name}... `);
      try {
        const resp = await httpGet(url);
        if (resp.startsWith("{") && resp.includes("not found")) {
          console.log("404 Not Found");
          continue;
        }
        const decrypted = decryptFn(resp, seed, tmdbId);
        const result = JSON.parse(decrypted);
        const sources = result.sources || [];
        if (sources.length === 0) {
          console.log("No sources");
          continue;
        }
        console.log(`${sources.length} source(s) found!`);
        for (const src of sources) {
          const urlStr = src.url || "N/A";
          console.log(`   🎥 [${src.quality || "?"}] ${src.type || "?"}: ${urlStr.substring(0, 150)}`);
        }
        if (result.subtitles && result.subtitles.length > 0) {
          console.log(`   📝 ${result.subtitles.length} subtitle(s) available`);
        }

        // Print the best direct URL
        const best = sources.find(s => s.url && (s.url.includes("1080") || s.quality === "1080p")) || sources[0];
        if (best && best.url) {
          console.log(`\n✅ DIRECT VIDEO URL (1080p):`);
          console.log(best.url);
        }
      } catch (err) {
        console.log(`Error: ${err.message}`);
      }
    }
  } catch (err) {
    console.error("Error:", err.message);
  }
})();
