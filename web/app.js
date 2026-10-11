/* Vietnam Atlas 2.0 — map UI backed by persistent SQLite API. */
const $ = id => document.getElementById(id);
const fallbackProvinces = ["An Giang","Bắc Ninh","Cà Mau","Cao Bằng","Đắk Lắk","Điện Biên","Đồng Nai","Đồng Tháp","Gia Lai","Hà Tĩnh","Hải Phòng","Hồ Chí Minh","Huế","Hưng Yên","Khánh Hòa","Lai Châu","Lâm Đồng","Lạng Sơn","Lào Cai","Nghệ An","Ninh Bình","Phú Thọ","Quảng Ngãi","Quảng Ninh","Quảng Trị","Sơn La","Tây Ninh","Thái Nguyên","Thanh Hóa","Tuyên Quang","Vĩnh Long","Đà Nẵng","Hà Nội","Cần Thơ"];
// These are navigation anchors only, not official provincial centroids or polygons.
const anchors = {
"Hà Nội":[21.0285,105.8542],"Hồ Chí Minh":[10.8231,106.6297],"Đà Nẵng":[16.0544,108.2022],"Hải Phòng":[20.8449,106.6881],"Cần Thơ":[10.0452,105.7469],"Huế":[16.4637,107.5909],
"An Giang":[10.5216,105.1259],"Bắc Ninh":[21.1861,106.0763],"Cà Mau":[9.1769,105.1524],"Cao Bằng":[22.6666,106.2639],"Đắk Lắk":[12.6667,108.0500],"Điện Biên":[21.3860,103.0230],"Đồng Nai":[10.9453,106.8243],"Đồng Tháp":[10.4938,105.6882],"Gia Lai":[13.9833,108.0000],"Hà Tĩnh":[18.3559,105.8877],"Hưng Yên":[20.6464,106.0511],"Khánh Hòa":[12.2388,109.1967],"Lai Châu":[22.3964,103.4582],"Lâm Đồng":[11.9404,108.4583],"Lạng Sơn":[21.8537,106.7610],"Lào Cai":[22.4856,103.9707],"Nghệ An":[18.6796,105.6813],"Ninh Bình":[20.2506,105.9745],"Phú Thọ":[21.3089,105.2045],"Quảng Ngãi":[15.1214,108.8044],"Quảng Ninh":[20.9712,107.0448],"Quảng Trị":[16.7403,107.1855],"Sơn La":[21.3256,103.9188],"Tây Ninh":[11.3352,106.1099],"Thái Nguyên":[21.5942,105.8482],"Thanh Hóa":[19.8067,105.7852],"Tuyên Quang":[21.8236,105.2142],"Vĩnh Long":[10.2396,105.9572]
};
const map = L.map('map',{zoomControl:false,preferCanvas:true}).setView([16.2,107.8],5.5);
L.control.zoom({position:'bottomright'}).addTo(map);
const layers = {
  osm:L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap contributors</a>'}),
  light:L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',{maxZoom:20,attribution:'&copy; OpenStreetMap contributors &copy; CARTO'}),
  topo:L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',{maxZoom:17,attribution:'&copy; OpenStreetMap contributors | OpenTopoMap, SRTM'})
};
layers.osm.addTo(map);
// Imagery is opt-in: the API only advertises sources configured by the operator.
let satelliteSource=null;
// Provider imagery stays with the provider; this client does not prefetch or cache tiles.
async function configureSatelliteLayer(){
  const button=document.querySelector('.layer[data-layer="satellite"]');
  try{
    const config=await api('/api/map-config');
    const sat=config.satellite;
    if(!sat){button.title='Thiết lập ATLAS_MAPTILER_KEY hoặc nguồn XYZ được cấp phép';return}
    satelliteSource=sat.source;
    layers.satellite=L.tileLayer(sat.url,{
      maxZoom:20,maxNativeZoom:Math.min(20,Math.max(0,Number(sat.maxNativeZoom)||19)),
      tileSize:sat.tileSize===512?512:256,zoomOffset:sat.tileSize===512?-1:0,
      attribution:sat.attribution,updateWhenIdle:true
    });
    let warned=false;
    layers.satellite.on('tileerror',()=>{
      if(!warned){warned=true;toast('Không tải được ảnh vệ tinh: kiểm tra API key, hạn mức hoặc máy chủ tile.')}
    });
    button.disabled=false;
    button.title='Ảnh vệ tinh từ nguồn '+sat.source+' (không phải ảnh trực tiếp thời gian thực)';
  }catch(_error){/* Ảnh vệ tinh là tùy chọn: không tải được cấu hình thì giữ nút tắt, không cần xử lý thêm. */button.title='Không tải được cấu hình ảnh vệ tinh'}
}
const pointIcon = L.divIcon({className:'',html:'<div style="width:17px;height:17px;border:3px solid white;border-radius:50%;background:#10a58f;box-shadow:0 2px 9px #0b46555e"></div>',iconSize:[17,17],iconAnchor:[8,8]});
let places=[],markers=new Map(),adding=false,toastTimer,loadSeq=0,searchTimer,roadOverlay=null;
let refreshAbort=null,moveTimer=null,liveTimer=null;
// "Gần tôi" state: once granted, refreshes (pan/zoom included) keep using near= instead of bbox.
let nearMe={active:false,lat:null,lon:null};
let panelPlaceId=null;
// Last nationwide query the map was focused for; panning/zooming must not re-fit.
let lastFocusKey='';
// Category prefix → Vietnamese label, derived from the #categoryFilter options so the
// dropdown and the panel badge can never drift apart.
const CATEGORY_LABELS={};
for(const opt of document.querySelectorAll('#categoryFilter option')){
  if(opt.value)for(const prefix of opt.value.split('|'))CATEGORY_LABELS[prefix]=opt.textContent;
}
const STATUS_LABELS={unverified:'Chưa xác minh',verified:'Đã xác minh',inactive:'Không còn hoạt động'};
function categoryLabel(cat){return CATEGORY_LABELS[cat]||cat||'Chưa phân loại'}
function statusLabel(s){return STATUS_LABELS[s]||s||'Chưa xác minh'}
function telHref(phone){return 'tel:'+String(phone??'').replace(/[^0-9+\-()\s]/g,'')}
// Only http(s) URLs are rendered as links — anything else is shown as plain text.
function websiteHref(url){const u=String(url??'').trim();return /^https?:\/\//i.test(u)?u:''}
function escapeHtml(v){return String(v??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]))}
function toast(message){const e=$('toast');e.textContent=message;e.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>e.classList.remove('show'),3300)}
async function api(path,options={}){
  // Every call site passes a literal '/api/...' path (user values go through
  // encodeURIComponent), so the target host can never be changed by input.
  if(!String(path).startsWith('/api/'))throw new Error('Đường dẫn API không hợp lệ');
  const res=await fetch(path,{...options,headers:{...(options.body?{'Content-Type':'application/json'}:{}),...(options.headers||{})}});// NOSONAR: path nội bộ, không nhận URL ngoài
  let body;try{body=await res.json()}catch{body={error:'Máy chủ không trả về JSON'}}
  if(!res.ok)throw new Error(body.error||`Lỗi HTTP ${res.status}`);
  return body;
}
function populateProvinces(names){for(const select of [$('province'),$('formProvince')]){const selected=select.value;select.length=1;names.forEach(name=>select.add(new Option(name,name)));select.value=selected}}
function currentBBox(){const b=map.getBounds();return [b.getWest(),b.getSouth(),b.getEast(),b.getNorth()].map(n=>n.toFixed(6)).join(',')}
// Nationwide mode: an active text search, category filter, or "gần tôi" drops the
// bbox so the whole country is searched (server contract: limit ≤1000). The old
// server ignores the extra params, so this stays safe pre-merge.
function nationwideMode(){
  return Boolean($('search').value.trim()||$('categoryFilter').value||(nearMe.active&&nearMe.lat!=null));
}
function placesQuery(){
  const search=$('search').value.trim();
  const category=$('categoryFilter').value;
  const nationwide=nationwideMode();
  const qs=new URLSearchParams({limit:nationwide?'100':'500'});
  if(!nationwide)qs.set('bbox',currentBBox());
  if(search)qs.set('search',search);
  if(category)qs.set('category',category);
  if(nearMe.active&&nearMe.lat!=null)qs.set('near',nearMe.lat.toFixed(6)+','+nearMe.lon.toFixed(6));
  if($('province').value)qs.set('province',$('province').value);
  return {qs,search,category,nationwide};
}
function placesCountText(data,query){
  const shown=places.length.toLocaleString('vi-VN');
  const found=data.total.toLocaleString('vi-VN');
  if(query.nationwide){
    const label=nearMe.active?'địa điểm gần bạn':'kết quả toàn quốc';
    const extra=data.total>places.length?` (tìm được ${found})`:'';
    return `${shown} ${label}${extra}`;
  }
  const extra=data.total>500?' (giới hạn 500/lần)':'';
  return `Đang hiển thị ${shown} / ${found} địa điểm trong vùng bản đồ${extra}`;
}
function focusNationwide(query){
  // Focus the map on nationwide result sets only when the query itself changed —
  // later pan/zoom refreshes reuse the same query and must not yank the viewport.
  if(!query.nationwide){lastFocusKey='';return}
  const key=[query.search,query.category,nearMe.active?'near':''].join('|');
  if(key===lastFocusKey)return;
  lastFocusKey=key;
  if(places.length>=2)map.fitBounds(L.latLngBounds(places.map(p=>[p.lat,p.lon])),{padding:[45,45]});
  else if(places.length===1)map.setView([places[0].lat,places[0].lon],15);
}
async function refresh(){
  const seq=++loadSeq;
  if(refreshAbort)refreshAbort.abort();
  const controller=new AbortController();
  refreshAbort=controller;
  try{
    const query=placesQuery();
    const data=await api('/api/places?'+query.qs,{signal:controller.signal});
    if(seq!==loadSeq)return;
    places=data.items;
    if(nearMe.active)places.sort((a,b)=>(a.distance_km??Infinity)-(b.distance_km??Infinity));
    renderMarkers();renderPlaces();
    $('visibleCount').textContent=placesCountText(data,query);
    $('serverStatus').textContent='Kho bộ nhớ đã kết nối';
    focusNationwide(query);
  }catch(e){
    if(seq===loadSeq&&e.name!=='AbortError'){$('serverStatus').textContent='Không kết nối được máy chủ';$('visibleCount').textContent=e.message;toast('Lỗi tải dữ liệu: '+e.message)}
  }
  finally{if(refreshAbort===controller)refreshAbort=null}
}
async function refreshStats(){try{const d=await api('/api/stats');$('count').textContent=d.total_places.toLocaleString('vi-VN')}catch{$('count').textContent='–'}}
function renderMarkers(){
  // Reuse markers on live refresh; only changed positions/popup data are updated.
  const keep=new Set();
  for(const p of places){
    keep.add(p.id);
    const popup=`<div class="popup-title">${escapeHtml(p.name)}</div><button class="popup-edit" data-popup-edit="${escapeHtml(p.id)}">Mở hồ sơ</button>`;
    let marker=markers.get(p.id);
    if(!marker){
      marker=L.marker([p.lat,p.lon],{icon:pointIcon}).addTo(map);
      marker.bindPopup(popup);marker._atlasPopup=popup;
      markers.set(p.id,marker);
    }else{
      const ll=marker.getLatLng();
      if(ll.lat!==p.lat||ll.lng!==p.lon)marker.setLatLng([p.lat,p.lon]);
      if(marker._atlasPopup!==popup){marker.setPopupContent(popup);marker._atlasPopup=popup;bindPopupButton(marker.getPopup()?.getElement())}
    }
  }
  for(const [id,marker] of markers){
    if(!keep.has(id)){map.removeLayer(marker);markers.delete(id)}
  }
}
function renderPlaces(){
  const container=$('places');
  if(!places.length){
    container.innerHTML=nationwideMode()
      ?'<div class="empty">Không tìm thấy địa điểm phù hợp.<br>Hãy thử từ khóa, loại hoặc tỉnh khác.</div>'
      :'<div class="empty">Không có địa điểm trong vùng hiển thị.<br>Hãy thêm điểm hoặc di chuyển bản đồ.</div>';
    return;
  }
  container.innerHTML=places.map(p=>{
    const dist=p.distance_km!=null?`<br>Cách ${Number(p.distance_km).toFixed(1)} km`:'';
    return `<div class="place-row" data-id="${escapeHtml(p.id)}"><strong>${escapeHtml(p.name)}</strong><div class="place-meta">${escapeHtml(categoryLabel(p.category))}<br>${escapeHtml(p.address||p.province||'Chưa có địa chỉ')}${dist}</div><div class="place-row-actions"><button class="mini" data-action="zoom">Xem</button><button class="mini" data-action="edit">Sửa</button><button class="mini" data-action="delete">Xóa</button></div></div>`;
  }).join('');
}
async function loadHistory(placeId){
  const container=$('historyList');container.textContent='Đang tải…';
  if(!placeId){container.textContent='Địa điểm chưa được lưu.';return}
  try{const data=await api('/api/places/'+encodeURIComponent(placeId)+'/history');
    container.innerHTML=data.items.length?data.items.map(row=>`<article><strong>${escapeHtml(row.action)}</strong> · ${escapeHtml(row.changed_at)}<br>Phiên bản #${row.seq}</article>`).join(''):'Chưa có lịch sử.';
  }catch(e){container.textContent=e.message}
}
async function loadFacts(placeId){
  for(const [kind,el] of [['memories','factsList'],['media','mediaList']]){
    const holder=$(el);
    if(!placeId){holder.textContent='Hãy lưu địa điểm trước.';continue}
    try{
      const data=await api('/api/places/'+encodeURIComponent(placeId)+'/'+kind);
      if(!data.items.length){holder.textContent='Chưa có dữ liệu.';continue}
      if(kind==='memories')holder.innerHTML=data.items.map(m=>`<article><strong>${escapeHtml(m.kind)}</strong> · ${escapeHtml(m.created_at)} · ${escapeHtml(m.source)}<br>${escapeHtml(m.content)}</article>`).join('');
      else holder.innerHTML=data.items.map(m=>`<article><a href="${escapeHtml(m.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(m.caption||'Xem ảnh / tài liệu')}</a><br>${escapeHtml(m.source)} · ${escapeHtml(m.rights_basis)}</article>`).join('');
    }catch(e){holder.textContent=e.message}
  }
}
function editor(latlng,p=null){
  $('placeForm').reset();$('placeId').value=p?.id||'';$('dialogTitle').textContent=p?'Sửa địa điểm':'Thêm địa điểm';
  for(const key of ['name','category','address','phone','website','notes','source','source_id']){$(key).value=p?.[key]||''}
  $('formProvince').value=p?.province||$('province').value||'';
  $('lat').value=p?.lat??latlng.lat.toFixed(6);$('lon').value=p?.lon??latlng.lng.toFixed(6);
  $('status').value=p?.status||'unverified';$('source').value=p?.source||'user';
  $('historyPanel').open=false;loadHistory(p?.id).catch(()=>{});loadFacts(p?.id).catch(()=>{});$('editor').showModal();
}
async function editById(id){
  try{const p=await api('/api/places/'+encodeURIComponent(id));editor({lat:p.lat,lng:p.lon},p)}catch(e){toast(e.message)}
}
function panelPlace(){return places.find(p=>p.id===panelPlaceId)}
function openPanel(p){
  if(!p)return;
  panelPlaceId=p.id;
  $('panelName').textContent=p.name||'Chưa có tên';
  $('panelCategory').textContent=categoryLabel(p.category);
  const st=$('panelStatus');st.textContent=statusLabel(p.status);st.dataset.status=p.status||'unverified';
  const addr=$('panelAddress');addr.textContent=p.address||'';addr.style.display=p.address?'':'none';
  $('panelProvince').textContent=p.province||'—';
  $('panelDistance').textContent=p.distance_km!=null?`Cách ${Number(p.distance_km).toFixed(1)} km`:'—';
  const phoneEl=$('panelPhone');phoneEl.textContent='';
  if(p.phone){const a=document.createElement('a');a.href=telHref(p.phone);a.textContent=String(p.phone);phoneEl.appendChild(a)}else{phoneEl.textContent='—'}
  const siteEl=$('panelWebsite');siteEl.textContent='';
  const href=websiteHref(p.website);
  if(href){const a=document.createElement('a');a.href=href;a.target='_blank';a.rel='noopener noreferrer';a.textContent=String(p.website);siteEl.appendChild(a)}else{siteEl.textContent='—'}
  $('panelCoords').textContent=`${Number(p.lat).toFixed(5)}, ${Number(p.lon).toFixed(5)}`;
  $('panelSource').textContent=[p.source,p.source_id?`#${p.source_id}`:''].filter(Boolean).join(' · ')||'—';
  $('panelUpdated').textContent=p.updated_at||'—';
  $('placePanel').classList.add('open');
  const url=new URL(location.href);url.searchParams.set('place',String(p.id));
  history.replaceState(null,'',url);
  loadPanelDetails(p.id).catch(()=>{});
}
function closePanel(){
  panelPlaceId=null;
  $('placePanel').classList.remove('open');
  const url=new URL(location.href);url.searchParams.delete('place');
  history.replaceState(null,'',url);
}
async function loadPanelDetails(id){
  const h=$('panelHistory');h.textContent='Đang tải…';
  try{
    const d=await api('/api/places/'+encodeURIComponent(id)+'/history');
    h.innerHTML=d.items.length?d.items.map(r=>`<article><strong>${escapeHtml(r.action)}</strong> · ${escapeHtml(r.changed_at)}<br>Phiên bản #${r.seq}</article>`).join(''):'Chưa có lịch sử.';
  }catch(e){h.textContent=e.message}
  const kinds=[['memories','panelMemories','Chưa có bộ nhớ chi tiết.'],['media','panelMedia','Chưa có ảnh/tài liệu.']];
  await Promise.all(kinds.map(([kind,el,empty])=>loadPanelKind(id,kind,$(el),empty)));
}
async function loadPanelKind(id,kind,holder,empty){
  holder.textContent='Đang tải…';
  try{
    const d=await api('/api/places/'+encodeURIComponent(id)+'/'+kind);
    if(!d.items.length){holder.textContent=empty;return}
    if(kind==='memories'){
      holder.innerHTML=d.items.map(m=>`<article><strong>${escapeHtml(m.kind)}</strong> · ${escapeHtml(m.created_at)} · ${escapeHtml(m.source)}<br>${escapeHtml(m.content)}</article>`).join('');
    }else{
      holder.innerHTML=d.items.map(m=>{
        const href=websiteHref(m.url);
        const cap=escapeHtml(m.caption||'Xem ảnh / tài liệu');
        const label=href?`<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${cap}</a>`:cap;
        return `<article>${label}<br>${escapeHtml(m.source)} · ${escapeHtml(m.rights_basis)}</article>`;
      }).join('');
    }
  }catch(e){holder.textContent=e.message}
}
$('panelClose').addEventListener('click',closePanel);
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&panelPlaceId)closePanel()});
$('panelDirections').addEventListener('click',()=>{
  const p=panelPlace();if(!p)return;
  window.open('https://www.google.com/maps/dir/?api=1&destination='+encodeURIComponent(Number(p.lat).toFixed(6)+','+Number(p.lon).toFixed(6)),'_blank','noopener');
});
$('panelCopyCoords').addEventListener('click',async()=>{
  const p=panelPlace();if(!p)return;
  const text=`${Number(p.lat).toFixed(6)}, ${Number(p.lon).toFixed(6)}`;
  toast(await copyText(text)?'Đã sao chép tọa độ.':'Không sao chép được tọa độ.');
});
$('panelCopyAddress').addEventListener('click',async()=>{
  const p=panelPlace();if(!p)return;
  const text=[p.address,p.province].filter(Boolean).join(', ');
  if(!text){toast('Địa điểm chưa có địa chỉ.');return}
  toast(await copyText(text)?'Đã sao chép địa chỉ.':'Không sao chép được địa chỉ.');
});
$('panelShare').addEventListener('click',async()=>{
  const p=panelPlace();if(!p)return;
  const url=location.origin+location.pathname+'?place='+encodeURIComponent(p.id);
  if(navigator.share){
    try{await navigator.share({title:p.name||'Vietnam Atlas',text:[p.name,p.address].filter(Boolean).join(' — '),url})}
    catch(e){if(e&&e.name!=='AbortError')toast('Không chia sẻ được.')}
    return;
  }
  toast(await copyText(url)?'Đã sao chép liên kết chia sẻ.':'Không sao chép được liên kết.');
});
$('panelEdit').addEventListener('click',()=>{if(panelPlaceId)editById(panelPlaceId).catch(()=>{})});
$('placeForm').addEventListener('submit',async e=>{
  if(e.submitter?.value==='cancel')return;
  e.preventDefault();
  const pid=$('placeId').value;
  const payload={};for(const k of ['name','category','address','phone','website','notes','source','source_id','status'])payload[k]=$(k).value.trim();
  payload.province=$('formProvince').value;payload.lat=+$('lat').value;payload.lon=+$('lon').value;
  if(!payload.name){toast('Tên địa điểm là bắt buộc');return}
  const save=$('save');save.disabled=true;
  try{
    await api(pid?'/api/places/'+encodeURIComponent(pid):'/api/places',{method:pid?'PUT':'POST',body:JSON.stringify(payload)});
    $('editor').close();map.setView([payload.lat,payload.lon],Math.max(map.getZoom(),13));
    await Promise.all([refresh(),refreshStats()]);
    if(panelPlaceId&&panelPlaceId===pid){try{const fresh=await api('/api/places/'+encodeURIComponent(pid));openPanel(fresh)}catch(_error){/* bỏ qua: panel sẽ hiển thị dữ liệu mới ở lần mở kế tiếp */}}
    toast(pid?'Đã lưu chỉnh sửa và lịch sử.':'Đã thêm địa điểm vào SQLite.');
  }catch(e){toast(e.message)}finally{save.disabled=false}
});
$('addFact').addEventListener('click',async()=>{
  const pid=$('placeId').value;
  if(!pid){toast('Hãy lưu địa điểm trước khi thêm bộ nhớ.');return}
  try{
    await api('/api/places/'+encodeURIComponent(pid)+'/memories',{method:'POST',body:JSON.stringify({kind:$('factKind').value,content:$('factContent').value,source:$('source').value||'user'})});
    $('factContent').value='';await loadFacts(pid);toast('Đã thêm bộ nhớ chi tiết.');
  }catch(e){toast(e.message)}
});
$('addMedia').addEventListener('click',async()=>{
  const pid=$('placeId').value;
  if(!pid){toast('Hãy lưu địa điểm trước khi thêm ảnh.');return}
  try{
    await api('/api/places/'+encodeURIComponent(pid)+'/media',{method:'POST',body:JSON.stringify({url:$('mediaUrl').value,rights_basis:$('mediaRights').value,source:$('source').value||'user'})});
    $('mediaUrl').value='';$('mediaRights').value='';await loadFacts(pid);toast('Đã lưu liên kết và thông tin quyền.');
  }catch(e){toast(e.message)}
});
map.on('click',e=>{if(adding){editor(e.latlng);adding=false;$('addMode').classList.remove('active')}});
map.on('moveend',()=>{clearTimeout(moveTimer);moveTimer=setTimeout(refresh,90)});
function bindPopupButton(root){
  const b=root?.querySelector('button[data-popup-edit]');
  if(!b||b.dataset.bound==='1')return;
  b.dataset.bound='1';b.addEventListener('click',e=>openPlaceById(e.currentTarget.dataset.popupEdit));
}
// Open the side panel for a place; uses the item already in the list when possible.
function openPlaceById(id){
  const p=places.find(x=>x.id===id);
  if(p)return openPanel(p);
  api('/api/places/'+encodeURIComponent(id)).then(openPanel).catch(e=>toast(e.message));
}
// Clipboard with a non-secure-context fallback (http://localhost deployments).
async function copyText(text){
  try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(text);return true}}catch(_error){/* Clipboard API bị chặn (tab không focus / thiếu quyền): rơi xuống fallback bên dưới */}
  try{
    const ta=document.createElement('textarea');
    ta.value=text;ta.setAttribute('readonly','');ta.style.position='fixed';ta.style.top='-9999px';
    document.body.appendChild(ta);ta.select();
    const ok=document.execCommand('copy');ta.remove();return ok; // NOSONAR: execCommand deprecated nhưng cần cho fallback non-secure context (LAN http)
  }catch(_error){return false} // trình duyệt chặn execCommand: coi như thất bại
}
map.on('popupopen',e=>bindPopupButton(e.popup.getElement()));
$('addMode').addEventListener('click',()=>{adding=!adding;$('addMode').classList.toggle('active',adding);toast(adding?'Bấm vị trí trên bản đồ để tạo hồ sơ.':'Đã tắt chế độ thêm điểm.')});
$('home').addEventListener('click',()=>map.setView([16.2,107.8],5.5));
document.querySelectorAll('.layer').forEach(b=>b.addEventListener('click',()=>{
  const selected=layers[b.dataset.layer];
  if(!selected)return toast("Lớp nền chưa được cấu hình.");
  Object.values(layers).forEach(layer=>map.removeLayer(layer));selected.addTo(map);
  const showMaptilerLogo=b.dataset.layer==="satellite"&&satelliteSource==="maptiler";$('maptilerLogo').hidden=!showMaptilerLogo;
  document.querySelectorAll('.layer').forEach(other=>other.classList.toggle('active',other===b));
}));
$('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(refresh,300)});
$('province').addEventListener('change',()=>{const p=$('province').value;if(p&&anchors[p])map.setView(anchors[p],8);else refresh().catch(()=>{})});
$('categoryFilter').addEventListener('change',()=>refresh().catch(()=>{}));
$('nearMe').addEventListener('click',()=>{
  const btn=$('nearMe');
  if(nearMe.active){nearMe={active:false,lat:null,lon:null};btn.classList.remove('active');btn.title='Tìm địa điểm gần vị trí của bạn';lastFocusKey='';refresh().catch(()=>{});return}
  if(!navigator.geolocation){toast('Trình duyệt không hỗ trợ định vị.');return}
  btn.disabled=true;
  navigator.geolocation.getCurrentPosition(pos=>{
    btn.disabled=false;nearMe={active:true,lat:pos.coords.latitude,lon:pos.coords.longitude};
    btn.classList.add('active');btn.title='Tắt tìm theo vị trí của bạn';lastFocusKey='';refresh().catch(()=>{});
  },err=>{
    btn.disabled=false;
    const detail=err?.message||'thử lại sau.';
    toast(err?.code===err.PERMISSION_DENIED?'Không lấy được vị trí: quyền định vị bị từ chối.':'Không lấy được vị trí: '+detail);
  },{enableHighAccuracy:false,timeout:8000,maximumAge:60000});
});
$('places').addEventListener('click',async e=>{
  const row=e.target.closest('.place-row');if(!row)return;
  const p=places.find(p=>p.id===row.dataset.id);if(!p)return;
  const act=e.target.dataset.action;
  if(act==='edit')return editById(p.id);
  if(act==='delete'){
    if(!confirm(`Xóa hồ sơ “${p.name}”? Lịch sử thay đổi vẫn được giữ trên máy chủ.`))return;
    try{
      await api('/api/places/'+encodeURIComponent(p.id),{method:'DELETE'});
      if(panelPlaceId===p.id)closePanel();
      await Promise.all([refresh(),refreshStats()]);toast('Đã xóa hồ sơ.');
    }catch(err){toast(err.message)}
    return;
  }
  if(act==='zoom')return openPanel(p);
  map.setView([p.lat,p.lon],Math.max(map.getZoom(),15));
  setTimeout(()=>markers.get(p.id)?.openPopup(),350);
});
$('export').addEventListener('click',()=>{window.location.href='/api/export.geojson?limit=20000'});
$('exportCsv').addEventListener('click',()=>{window.location.href='/api/export.csv'});
$('import').addEventListener('change',async e=>{
  const f=e.target.files[0];if(!f)return;
  try{
    const data=JSON.parse(await f.text());
    if(data.type!=='FeatureCollection')throw new Error('Chỉ hỗ trợ GeoJSON FeatureCollection');
    if(!confirm(`Nhập ${data.features?.length||0} điểm vào bộ nhớ SQLite? Dữ liệu trùng ID/nguồn có thể được cập nhật.`))return;
    const result=await api('/api/import',{method:'POST',body:JSON.stringify(data)});
    await Promise.all([refresh(),refreshStats()]);
    toast(`Nhập xong: ${result.created} mới, ${result.updated} cập nhật, ${result.rejected} lỗi.`);
  }catch(err){toast('Nhập thất bại: '+err.message)}finally{e.target.value=''}
});
$('roadsFile').addEventListener('change',async e=>{
  const file=e.target.files[0];if(!file)return;
  try{
    if(file.size>25*1024*1024)throw new Error('Lớp đường xem trước tối đa 25 MB. Trích xuất vùng nhỏ hơn.');
    const data=JSON.parse(await file.text());
    if(data.type!=='FeatureCollection'||!Array.isArray(data.features))throw new Error('GeoJSON không hợp lệ');
    if(data.features.length>20000)throw new Error('Lớp đường tối đa 20.000 đối tượng; cần tile server cho cả nước.');
    const onlyRoads={type:'FeatureCollection',features:data.features.filter(f=>['LineString','MultiLineString'].includes(f.geometry?.type))};
    if(roadOverlay)map.removeLayer(roadOverlay);
    roadOverlay=L.geoJSON(onlyRoads,{style:()=>({color:'#1768a5',weight:2,opacity:0.85}),interactive:false}).addTo(map);
    if(roadOverlay.getLayers().length)map.fitBounds(roadOverlay.getBounds(),{padding:[30,30]});
    toast(`Đã nạp ${roadOverlay.getLayers().length} đoạn đường GeoJSON`);
  }catch(err){toast(err.message)}finally{e.target.value=''}
});
$('clearRoads').addEventListener('click',()=>{if(roadOverlay){map.removeLayer(roadOverlay);roadOverlay=null;toast('Đã ẩn lớp đường tự nạp.')}});
$('refresh').addEventListener('click',()=>Promise.all([refresh(),refreshStats()]).catch(()=>{}));
function invalidateLive(){
  // Leading-edge coalescing: while a refresh is pending, further events are
  // dropped on purpose — the pending fetch already reads the newest state,
  // and resetting the timer on every event would starve continuous edits.
  if(document.hidden||liveTimer)return;
  liveTimer=setTimeout(()=>{liveTimer=null;Promise.all([refresh(),refreshStats()]).catch(()=>{})},120);
}
document.addEventListener('visibilitychange',()=>{if(!document.hidden)invalidateLive()});
function connectLiveUpdates(){
  if(typeof EventSource==='undefined'){
    setInterval(invalidateLive,6000);return;
  }
  const events=new EventSource('/api/live');
  events.addEventListener('ready',invalidateLive);
  events.addEventListener('changed',invalidateLive);
  events.addEventListener('error',()=>{
    // A rejected stream (503 at capacity) closes EventSource for good — it
    // never retries. Open a fresh connection instead of going silent.
    if(events.readyState===EventSource.CLOSED)setTimeout(connectLiveUpdates,5000);
  });
  // EventSource reconnects with Last-Event-ID; the existing REST endpoint remains the source of truth.
  window.addEventListener('beforeunload',()=>events.close());
}
(async()=>{
  try{const d=await api('/api/provinces');populateProvinces(d.items)}catch{populateProvinces(fallbackProvinces)}
  await Promise.all([configureSatelliteLayer(),refresh(),refreshStats()]);
  connectLiveUpdates();
  // Deep link: open ?place=ID straight into the profile panel.
  const pid=new URLSearchParams(location.search).get('place');
  if(pid){try{const p=await api('/api/places/'+encodeURIComponent(pid));openPanel(p)}catch(e){toast(e.message)}}
})();
