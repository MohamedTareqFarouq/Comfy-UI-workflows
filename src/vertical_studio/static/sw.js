const CACHE="vvs-0.8";
const ASSETS=["/","/styles.css?v=guided-retry-1","/app.js?v=guided-retry-1","/manifest.webmanifest"];
self.addEventListener("install",event=>{event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(ASSETS)).then(()=>self.skipWaiting()))});
self.addEventListener("activate",event=>{event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key!==CACHE).map(key=>caches.delete(key)))).then(()=>self.clients.claim()))});
self.addEventListener("fetch",event=>{const url=new URL(event.request.url);if(event.request.method!=="GET"||!["http:","https:"].includes(url.protocol)||event.request.url.includes("/api/"))return;event.respondWith(fetch(event.request).then(response=>{const copy=response.clone();caches.open(CACHE).then(cache=>cache.put(event.request,copy));return response}).catch(()=>caches.match(event.request))) });
