const $ = id => document.getElementById(id);
const state = { employees: [], roster: [], currentBranch: "" };
const weekdayArabic = {Saturday:"السبت",Sunday:"الأحد",Monday:"الاثنين",Tuesday:"الثلاثاء",Wednesday:"الأربعاء",Thursday:"الخميس",Friday:"الجمعة"};
const todayISO = () => { const d=new Date(); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`; };
const money = n => `${Number(n||0).toLocaleString("en-US",{maximumFractionDigits:2})} ج.م`;
const escapeHtml = s => String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

let currentPage = 1;
const itemsPerPage = 15;
let filteredRoster = [];
let chartInstance = null;

async function api(path, options={}) {
  const headers = {...(options.headers||{})};
  if(options.body){headers["Content-Type"]="application/json";}
  const response = await fetch(path,{...options,headers});
  const type=response.headers.get("content-type")||"";
  if(!response.ok){let message="حدث خطأ";try{const e=await response.json();message=e.detail||message}catch{}throw new Error(message);}
  if(type.includes("application/json"))return response.json();
  return response;
}

function toast(message){const el=$("toast");el.textContent=message;el.classList.add("show");setTimeout(()=>el.classList.remove("show"),2800);}
function showApp(){ $("todayPill").textContent=new Date().toLocaleDateString("ar-EG",{weekday:"short",day:"numeric",month:"short",year:"numeric"}); }

document.querySelectorAll(".nav-item").forEach(b=>b.addEventListener("click",()=>goPage(b.dataset.page)));
document.querySelectorAll("[data-goto]").forEach(b=>b.addEventListener("click",()=>{if(b.dataset.branch)state.currentBranch=b.dataset.branch;goPage(b.dataset.goto);if(b.dataset.branch)$("attendanceBranch").value=b.dataset.branch;loadRoster();}));

function goPage(page){document.querySelectorAll(".page").forEach(p=>p.classList.add("hidden"));$("page-"+page).classList.remove("hidden");document.querySelectorAll(".nav-item").forEach(b=>b.classList.toggle("active",b.dataset.page===page));const titles={dashboard:"نظرة عامة",attendance:"الحضور اليومي",employees:"إدارة الموظفين",reports:"التقارير"};$("pageTitle").textContent=titles[page]||"";if(page==="employees")loadEmployees();if(page==="attendance")loadRoster();if(page==="dashboard")loadDashboard();if(page==="reports")runReport();}

async function init(){
  try {
    showApp();
    $("attendanceDate").value=todayISO();
    $("dashboardDate").value=todayISO();
    $("reportEnd").value=todayISO();
    const d=new Date();
    d.setDate(d.getDate()-6);
    $("reportStart").value=d.toISOString().slice(0,10);
    await loadEmployees();
    await loadDashboard();
    await loadRoster();
  } catch(e) { toast(e.message); }
}

async function loadEmployees(){try{state.employees=await api(`/api/employees?active_only=${$("showInactive")?.checked?"false":"false"}`);renderEmployees();populateShifts();}catch(e){toast(e.message);}}

function renderEmployees(){const q=($("employeeSearch").value||"").toLowerCase();const branch=$("employeeBranch").value;const type=$("employeeType").value;const inactive=$("showInactive").checked;const list=state.employees.filter(e=>(inactive||e.active)&&(!branch||e.branch===branch)&&(!type||e.employment_type===type)&&(`${e.name} ${e.employee_code}`.toLowerCase().includes(q)));$("employeeRows").innerHTML=list.map(e=>`<tr><td><div class="employee-name">${escapeHtml(e.name)}</div><div class="employee-code">${escapeHtml(e.employee_code||"بدون كود")}</div></td><td>${escapeHtml(e.branch)}</td><td>${escapeHtml(e.shift)}</td><td><span class="tag ${e.employment_type.toLowerCase()}">${e.employment_type==="FT"?"Full Time":"Part Time"}</span></td><td>${weekdayArabic[e.day_off]||e.day_off}</td><td><span class="tag ${e.active?"active":"inactive"}">${e.active?"نشط":"غير نشط"}</span></td><td><div class="actions"><button class="mini-btn" data-edit="${e.id}">تعديل</button>${e.active?`<button class="mini-btn danger" data-deactivate="${e.id}">إيقاف</button>`:""}</div></td></tr>`).join("");$("employeeEmpty").classList.toggle("hidden",list.length>0);$("employeeRows").querySelectorAll("[data-edit]").forEach(b=>b.onclick=()=>openEmployee(+b.dataset.edit));$("employeeRows").querySelectorAll("[data-deactivate]").forEach(b=>b.onclick=()=>deactivateEmployee(+b.dataset.deactivate));}
["employeeSearch","employeeBranch","employeeType","showInactive"].forEach(id=>$(id).addEventListener(id==="employeeSearch"?"input":"change",renderEmployees));
function populateShifts(){const sel=$("attendanceShift");const current=sel.value;const shifts=[...new Set(state.employees.filter(e=>e.active).map(e=>e.shift))].sort();sel.innerHTML='<option value="">كل الشيفتات</option>'+shifts.map(s=>`<option>${escapeHtml(s)}</option>`).join("");if(shifts.includes(current))sel.value=current;}

function openEmployee(id=null){$("employeeFormError").textContent="";$("employeeForm").dataset.mode=id?"edit":"create";$("employeeModalTitle").textContent=id?"تعديل بيانات الموظف":"إضافة موظف";$("employeeForm").reset();$("employeeId").value="";$("empActive").checked=true;$("empDayOff").value="Friday";if(id){const e=state.employees.find(x=>x.id===id);if(!e)return;$("employeeId").value=e.id;$("empName").value=e.name;$("empCode").value=e.employee_code||"";$("empBranch").value=e.branch;$("empType").value=e.employment_type;$("empShift").value=e.shift;$("empDayOff").value=e.day_off;$("empActive").checked=e.active;} $("employeeDialog").showModal();}
$("addEmployeeBtn").onclick=()=>openEmployee();$("closeEmployeeModal").onclick=()=>$("employeeDialog").close();$("cancelEmployee").onclick=()=>$("employeeDialog").close();

$("employeeForm").addEventListener("submit",async e=>{e.preventDefault();const data={name:$("empName").value.trim(),employee_code:$("empCode").value.trim()\vert{}\vert{}null,branch:$("empBranch").value,employment_type:$("empType").value,shift:$("empShift").value.trim(),day_off:$("empDayOff").value,active:$("empActive").checked};const id=$("employeeId").value;try{await api(id?`/api/employees/${id}`:"/api/employees",{method:id?"PUT":"POST",body:JSON.stringify(data)});$("employeeDialog").close();toast("تم حفظ بيانات الموظف");await loadEmployees();await loadDashboard();await loadRoster();}catch(err){$("employeeFormError").textContent=err.message;}});
async function deactivateEmployee(id){if(!confirm("سيتم إيقاف الموظف مع الاحتفاظ بسجل الحضور السابق. هل تريد المتابعة؟"))return;try{await api(`/api/employees/${id}`,{method:"DELETE"});toast("تم إيقاف الموظف");await loadEmployees();await loadDashboard();await loadRoster();}catch(e){toast(e.message);}}

$("attendanceDate").addEventListener("change",loadRoster);$("attendanceBranch").addEventListener("change",loadRoster);$("attendanceShift").addEventListener("change",renderRoster);$("attendanceType").addEventListener("change",renderRoster);$("refreshRoster").onclick=loadRoster;

async function loadRoster(){const date=$("attendanceDate").value||todayISO();const branch=$("attendanceBranch").value;try{const r=await api(`/api/roster?attendance_date=${date}${branch?"&branch="+encodeURIComponent(branch):""}`);state.roster=r.employees;currentPage=1;renderRoster();}catch(e){toast(e.message);}}

function renderRoster() {
  const shift = $("attendanceShift").value, type = $("attendanceType").value;
  filteredRoster = state.roster.filter(e => (!shift || e.shift === shift) && (!type || e.employment_type === type));
  
  const totals = { Present: 0, Absent: 0, Late: 0, Unmarked: 0 };
  filteredRoster.forEach(e => {
    const s = e.attendance?.status;
    if (s) totals[s]++; else totals.Unmarked++;
  });
  
  $("attendanceSummary").innerHTML = `<span class="summary-chip">المجدولون <b>${filteredRoster.length}</b></span><span class="summary-chip">حاضر <b>${totals.Present}</b></span><span class="summary-chip">غائب <b>${totals.Absent}</b></span><span class="summary-chip">متأخر <b>${totals.Late}</b></span><span class="summary-chip">لم يُسجل <b>${totals.Unmarked}</b></span>`;
  updatePagination();
}

function updatePagination() {
  const totalPages = Math.ceil(filteredRoster.length / itemsPerPage) || 1;
  if (currentPage > totalPages) currentPage = totalPages;
  const start = (currentPage - 1) * itemsPerPage;
  const paginatedList = filteredRoster.slice(start, start + itemsPerPage);
  
  $("attendanceEmpty").classList.toggle("hidden", filteredRoster.length > 0);
  $("paginationControls").classList.toggle("hidden", filteredRoster.length <= itemsPerPage);
  $("pageInfo").textContent = `صفحة ${currentPage} من ${totalPages}`;
  $("prevPage").disabled = currentPage === 1;
  $("nextPage").disabled = currentPage === totalPages;
  
  $("attendanceList").innerHTML = paginatedList.map(e => {
    const a = e.attendance || {};
    const status = a.status || "";
    return `<tr data-employee="${e.id}">
      <td><div class="employee-name">${escapeHtml(e.name)}</div><div class="employee-code">${escapeHtml(e.branch)} · ${escapeHtml(e.shift)}</div></td>
      <td>
        <select class="att-status-select compact-input" style="width:100px">
          <option value="">اختر</option>
          <option value="Present" ${status === "Present" ? "selected" : ""}>حاضر</option>
          <option value="Absent" ${status === "Absent" ? "selected" : ""}>غائب</option>
          <option value="Late" ${status === "Late" ? "selected" : ""}>متأخر</option>
        </select>
      </td>
      <td><input class="att-fine compact-input" type="number" min="0" value="${escapeHtml(a.fine ?? 0)}" style="width:70px" ${status === "Present" ? "disabled" : ""}></td>
      <td><input class="att-reason compact-input" value="${escapeHtml(a.reason || "")}" placeholder="السبب..." ${status === "Present" ? "disabled" : ""}></td>
      <td><input class="att-note compact-input" value="${escapeHtml(a.note || "")}" placeholder="ملاحظة..."></td>
      <td><button class="save-att ${a.id ? "saved" : ""} compact-input" data-save="${e.id}">${a.id ? "✓ تحديث" : "حفظ"}</button></td>
    </tr>`;
  }).join("");
  attachAttendanceEvents();
}

$("prevPage").onclick = () => { if(currentPage > 1) { currentPage--; updatePagination(); } };
$("nextPage").onclick = () => { const max = Math.ceil(filteredRoster.length / itemsPerPage); if(currentPage < max) { currentPage++; updatePagination(); } };

function attachAttendanceEvents() {
  $("attendanceList").querySelectorAll(".att-status-select").forEach(s => s.addEventListener("change", () => {
    const row = s.closest("tr");
    const disabled = s.value === "Present";
    row.querySelector(".att-fine").disabled = disabled;
    row.querySelector(".att-reason").disabled = disabled;
    if (disabled) { row.querySelector(".att-fine").value = 0; row.querySelector(".att-reason").value = ""; }
  }));
  $("attendanceList").querySelectorAll("[data-save]").forEach(b => b.onclick = () => saveAttendance(+b.dataset.save, b));
}

async function saveAttendance(id, button) {
  const row = button.closest("tr");
  const status = row.querySelector(".att-status-select").value;
  if (!status) { toast("اختر حالة الحضور أولًا"); return; }
  const fine = Number(row.querySelector(".att-fine").value || 0);
  const reason = row.querySelector(".att-reason").value.trim();
  if (status !== "Present" && fine > 0 && !reason) { toast("اكتب سبب الغياب"); row.querySelector(".att-reason").focus(); return; }
  const data = { employee_id: id, attendance_date: $("attendanceDate").value || todayISO(), status, fine: status === "Present" ? 0 : fine, reason: status === "Present" ? "" : reason, note: row.querySelector(".att-note").value.trim() };
  
  button.disabled = true; button.textContent = "...";
  try {
    await api("/api/attendance", { method: "POST", body: JSON.stringify(data) });
    button.textContent = "✓ تم"; button.classList.add("saved"); toast("تم حفظ التسجيل");
    await loadDashboard(); 
  } catch (e) { toast(e.message); button.disabled = false; button.textContent = "حفظ"; }
}

$("dashboardDate").addEventListener("change",loadDashboard);

async function loadDashboard(){
  const date=$("dashboardDate").value||todayISO();
  try{
    const [emps,roster,perf]=await Promise.all([
        api("/api/employees?active_only=true"),
        api(`/api/roster?attendance_date=${date}`),
        api("/api/reports/performance")
    ]);
    const rows=roster.employees;
    const count={Present:0,Absent:0,Late:0};
    let q1=0,q2=0,f1=0,f2=0;
    rows.forEach(e=>{if(e.branch==="QCD 1")q1++;else q2++;if(e.attendance){count[e.attendance.status]=(count[e.attendance.status]||0)+1;if(e.branch==="QCD 1")f1+=Number(e.attendance.fine||0);else f2+=Number(e.attendance.fine||0);}});
    $("statEmployees").textContent=emps.length;$("statPresent").textContent=count.Present||0;$("statAbsent").textContent=count.Absent\vert{}\vert{}0;$("statLate").textContent=count.Late||0;$("qcd1Count").textContent=q1;$("qcd2Count").textContent=q2;$("qcd1Fine").textContent=money(f1);$("qcd2Fine").textContent=money(f2);
    
    $("topPerformers").innerHTML = perf.top.length ? perf.top.map(p => `<li><div><strong>${escapeHtml(p.name)}</strong><br><span>${escapeHtml(p.branch)}</span></div><span>حاضر: ${p.present}</span></li>`).join("") : '<li>لا توجد بيانات كافية</li>';
    $("needsAttention").innerHTML = perf.attention.length ? perf.attention.map(p => `<li><div><strong>${escapeHtml(p.name)}</strong><br><span>${escapeHtml(p.branch)}</span></div><span class="error">غياب/تأخير: ${p.absent + p.late}</span></li>`).join("") : '<li>لا توجد حالات مسجلة</li>';

  }catch(e){toast(e.message);}
}

$("runReport").onclick=runReport;
$("exportReport").onclick=async()=>{try{const start=$("reportStart").value,end=$("reportEnd").value,branch=$("reportBranch").value;if(!start||!end)throw new Error("اختر فترة التقرير");const res=await api(`/api/reports/export.csv?start=${start}&end=${end}${branch?"&branch="+encodeURIComponent(branch):""}`);const blob=await res.blob();const url=URL.createObjectURL(blob);const a=document.createElement("a");a.href=url;a.download=`bankai_attendance_${start}_${end}.csv`;a.click();URL.revokeObjectURL(url);}catch(e){toast(e.message);}};

async function runReport(){
  const start=$("reportStart").value,end=$("reportEnd").value,branch=$("reportBranch").value;
  if(!start||!end)return;
  try{
    const r=await api(`/api/reports/summary?start=${start}&end=${end}${branch?"&branch="+encodeURIComponent(branch):""}`);
    $("rTotal").textContent=r.total;$("rPresent").textContent=r.present;$("rAbsent").textContent=r.absent;$("rLate").textContent=r.late;$("rFine").textContent=money(r.fine_total);
    const names={"QCD 1":"الفرع الأول · QCD 1","QCD 2":"الفرع الثاني · QCD 2"};
    $("branchReport").innerHTML=Object.entries(r.by_branch).length?Object.entries(r.by_branch).map(([name,b])=>`<div class="branch-report-card"><h4>${names[name]||name}</h4><p><span>إجمالي التسجيلات</span><b>${b.total}</b></p><p><span>حضور</span><b>${b.present}</b></p><p><span>غياب</span><b>${b.absent}</b></p><p><span>تأخير</span><b>${b.late}</b></p><p><span>الغرامات</span><b>${money(b.fine_total)}</b></p></div>`).join(""):'<p class="muted">لا توجد تسجيلات في الفترة المحددة.</p>';
    
    const ctx = document.getElementById('attendanceChart').getContext('2d');
    if (chartInstance) chartInstance.destroy();
    chartInstance = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: ['حضور', 'غياب', 'تأخير'],
        datasets: [{ data: [r.present, r.absent, r.late], backgroundColor: ['#16845b', '#c83e4d', '#a9670b'], borderWidth: 0 }]
      },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'right', labels: { font: { family: "'Cairo', sans-serif" } } } } }
    });
  }catch(e){toast(e.message);}
}

// تشغيل النظام مباشرة
init();
