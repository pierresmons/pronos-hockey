/* Pronos Hockey : service worker des rappels (notifications push).
   Pas de cache : l'app est toujours lue en ligne. Version 2 (10/11/2026). */
self.addEventListener("install", function(){ self.skipWaiting(); });
self.addEventListener("activate", function(e){ e.waitUntil(self.clients.claim()); });
self.addEventListener("push", function(e){
  var d = {};
  try{ d = e.data ? e.data.json() : {}; }catch(x){ d = {body: e.data ? e.data.text() : ""}; }
  var o = {body: d.body || "", tag: d.tag || "pronos", data: {url: d.url || "./championnat.html"}};
  /* Version 2 (10/11/2026) : logo de club (icon) et grande image (image) si le message en contient ;
     Android et ordinateurs les affichent, iPhone a priori non (a verifier avec le mode test). */
  if(d.icon) o.icon = d.icon;
  if(d.image) o.image = d.image;
  e.waitUntil(self.registration.showNotification(d.title || "Pronos Hockey", o));
});
self.addEventListener("notificationclick", function(e){
  e.notification.close();
  var url = new URL((e.notification.data && e.notification.data.url) || "./championnat.html", self.registration.scope).href;
  e.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(function(list){
    for(var i = 0; i < list.length; i++){
      if(list[i].url.split("?")[0] === url.split("?")[0] && "focus" in list[i]) return list[i].focus();
    }
    return self.clients.openWindow(url);
  }));
});
