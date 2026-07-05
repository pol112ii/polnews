// 항상 네트워크 우선 — 뉴스 데이터가 낡은 캐시로 나오지 않도록 한다.
// 네트워크가 안 될 때만 마지막으로 봤던 화면을 보여주는 오프라인 대비용.
const CACHE = "polnews-v1";

self.addEventListener("install", (e) => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(clients.claim()));

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(e.request))
  );
});
