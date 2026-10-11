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
function escapeHtml(v){return String(v??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]))}
function toast(message){const e=$('toast');e.textContent=message;e.classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>e.classList.remove('show'),3300)}
async function api(path,options={}){
  const res=await fetch(path,{...options,headers:{...(options.body?{'Content-Type':'application/json'}:{}),...(options.headers||{})}});
  let body;try{body=await res.json()}catch{body={error:'Máy chủ không trả về JSON'}}
  if(!res.ok)throw new Error(body.error||`Lỗi HTTP ${res.status}`);
  return body;
}
function populateProvinces(names){for(const select of [$('province'),$('formProvince')]){const selected=select.value;select.length=1;names.forEach(name=>select.add(new Option(name,name)));select.value=selected}}
function currentBBox(){const b=map.getBounds();return [b.getWest(),b.getSouth(),b.getEast(),b.getNorth()].map(n=>n.toFixed(6)).join(',')}
async function refresh(){
  const seq=++loadSeq;
  try{
    const qs=new URLSearchParams({limit:'500',bbox:currentBBox()});
    if($('search').value.trim())qs.set('search',$('search').value.trim());
    if($('province').value)qs.set('province',$('province').value);
    const data=await api('/api/places?'+qs);
    if(seq!==loadSeq)return;
    places=data.items;renderMarkers();renderPlaces();
    $('visibleCount').textContent=`Đang hiển thị ${places.length.toLocaleString('vi-VN')} / ${data.total.toLocaleString('vi-VN')} địa điểm trong vùng bản đồ${data.total>500?' (giới hạn 500/lần)':''}`;
    $('serverStatus').textContent='Kho bộ nhớ đã kết nối';
  }catch(e){if(seq!==loadSeq)return;$('serverStatus').textContent='Không kết nối được máy chủ';$('visibleCount').textContent=e.message;toast('Lỗi tải dữ liệu: '+e.message)}
}
async function refreshStats(){try{const d=await api('/api/stats');$('count').textContent=d.total_places.toLocaleString('vi-VN')}catch{$('count').textContent='–'}}
function renderMarkers(){
  markers.forEach(m=>map.removeLayer(m));markers.clear();
  for(const p of places){
    const m=L.marker([p.lat,p.lon],{icon:pointIcon}).addTo(map);
    const popup=`<div class="popup-title">${escapeHtml(p.name)}</div><div class="popup-meta">${escapeHtml(p.category||'Chưa phân loại')}<br>${escapeHtml(p.address||'Chưa có địa chỉ')}<br>${escapeHtml(p.province||'Chưa xác định')} · ${escapeHtml(p.status)}</div><button class="popup-edit" data-popup-edit="${escapeHtml(p.id)}">Mở hồ sơ</button>`;
    m.bindPopup(popup);markers.set(p.id,m);
  }
}
function renderPlaces(){
  const container=$('places');
  if(!places.length){container.innerHTML='<div class="empty">Không có địa điểm trong vùng hiển thị.<br>Hãy thêm điểm hoặc di chuyển bản đồ.</div>';return}
  container.innerHTML=places.map(p=>`<div class="place-row" data-id="${escapeHtml(p.id)}"><strong>${escapeHtml(p.name)}</strong><div class="place-meta">${escapeHtml(p.category||'Chưa phân loại')}<br>${escapeHtml(p.address||p.province||'Chưa có địa chỉ')}</div><div class="place-row-actions"><button class="mini" data-action="zoom">Xem</button><button class="mini" data-action="edit">Sửa</button><button class="mini" data-action="delete">Xóa</button></div></div>`).join('');
}
async function history(placeId){
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
  $('historyPanel').open=false;history(p?.id);loadFacts(p?.id);$('editor').showModal();
}
async function editById(id){
  try{const p=await api('/api/places/'+encodeURIComponent(id));editor({lat:p.lat,lng:p.lon},p)}catch(e){toast(e.message)}
}
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
    await Promise.all([refresh(),refreshStats()]);toast(pid?'Đã lưu chỉnh sửa và lịch sử.':'Đã thêm địa điểm vào SQLite.');
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
map.on('moveend',()=>refresh());
map.on('popupopen',()=>{document.querySelector('.leaflet-popup-content button[data-popup-edit]')?.addEventListener('click',e=>editById(e.currentTarget.dataset.popupEdit))});
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
$('province').addEventListener('change',()=>{const p=$('province').value;if(p&&anchors[p])map.setView(anchors[p],8);else refresh()});
$('places').addEventListener('click',async e=>{
  const row=e.target.closest('.place-row');if(!row)return;
  const p=places.find(p=>p.id===row.dataset.id);if(!p)return;
  const act=e.target.dataset.action;
  if(act==='edit')return editById(p.id);
  if(act==='delete'){
    if(!confirm(`Xóa hồ sơ “${p.name}”? Lịch sử thay đổi vẫn được giữ trên máy chủ.`))return;
    try{await api('/api/places/'+encodeURIComponent(p.id),{method:'DELETE'});await Promise.all([refresh(),refreshStats()]);toast('Đã xóa hồ sơ.')}catch(err){toast(err.message)}
    return;
  }
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
$('refresh').addEventListener('click',()=>Promise.all([refresh(),refreshStats()]));
(async()=>{
  try{const d=await api('/api/provinces');populateProvinces(d.items)}catch{populateProvinces(fallbackProvinces)}
  await Promise.all([configureSatelliteLayer(),refresh(),refreshStats()]);
})();
