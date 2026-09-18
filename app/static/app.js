const DASHBOARD_REFRESH_MS = 500;
const ADMIN_USERS_REFRESH_MS = 1000;
const MAX_CALCULATOR_SCENARIO_POSITIONS = 500;
const form = document.querySelector("#settingsForm");
const scheduleForm = document.querySelector("#scheduleForm");
const userTradingModeForm = document.querySelector("#userTradingModeForm");
const calculatorForm = document.querySelector("#calculatorForm");
const stopMode = document.querySelector("#stopMode");
const toast = document.querySelector("#toast");

const CALCULATOR_DEFAULTS = Object.freeze({
  martingale_enabled: true,
  initial_lot: 0.01,
  lot_multiplier: 1.5,
  martingale_distance_pips: 15,
  max_martingale: 5,
  max_lot: 0.5,
  pip_value_usd: 10,
  tp_pips_from_bep: 20,
  sl_pips_from_last: 20,
});

let currentUser = null;
let activeEaId = null;
let adminUsers = [];
let formDirty = false;
let scheduleDirty = false;
let dailyProfitAutoStopDirty = false;
let stopModeDirty = false;
let serverStopMode = "delete_pending";
let busy = false;
let dashboardRefreshPromise = null;
let scheduleClockOffsetMs = 0;
let scheduleNextAtMs = null;
let scheduleNextAction = null;
let pendingDeleteUsername = null;
let tradingModes = [];
let tradingModeBusy = false;
let tradingModeDirty = false;
let activeTradingModeName = null;
let activeDashboardData = null;
let calculatorEaId = null;
let calculatorCurrency = "USD";
let adminUsersRefreshPromise = null;
let dashboardEventSource = null;
let lastDashboardStreamAt = 0;
let accountEaId = null;
let accountExpirationAtMs = null;
let accountExpirationUnlimited = false;
let accountExpirationRequired = false;
let accountExpirationEnabled = false;
let toastTimer;

const numberFields = new Set([
  "rsi_period", "rsi_buy_level", "rsi_sell_level", "initial_lot",
  "sl_pips_from_last", "tp_pips_from_bep", "martingale_distance_pips",
  "lot_multiplier", "max_martingale", "max_lot", "initial_sl_pips", "first_layer_tp_pips",
  "trailing_start_pips", "trailing_distance_pips", "trailing_step_pips",
]);
const decimalConfigFields = new Set(["initial_lot", "lot_multiplier", "max_lot"]);

function byId(id) { return document.getElementById(id); }
function setText(id, value) { byId(id).textContent = value; }

function strategyLabel(strategyType) {
  return strategyType === "trailing" ? "RSI Martingale Trailing" : "RSI Martingale";
}

function setStrategyFields(strategyType = "standard") {
  const trailing = strategyType === "trailing";
  for (const section of document.querySelectorAll(".strategy-standard-field")) {
    section.hidden = trailing;
    section.querySelectorAll("input, select, button").forEach((control) => { control.disabled = trailing; });
  }
  for (const section of document.querySelectorAll(".strategy-trailing-field")) {
    section.hidden = !trailing;
    section.querySelectorAll("input, select, button").forEach((control) => { control.disabled = !trailing; });
  }
}

function jakartaDateTimeLocal(isoValue) {
  if (!isoValue) return "";
  const date = new Date(isoValue);
  if (Number.isNaN(date.getTime())) return "";
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Jakarta",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date).filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}T${parts.hour}:${parts.minute}`;
}

function jakartaLocalToIso(value) {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(String(value || ""))) return null;
  const parsed = new Date(`${value}:00+07:00`);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

function expirationDateLabel(isoValue) {
  const date = new Date(isoValue);
  if (Number.isNaN(date.getTime())) return "Waktu tidak valid";
  return new Intl.DateTimeFormat("id-ID", {
    timeZone: "Asia/Jakarta",
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function expirationRemainingLabel(milliseconds) {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  return `${days} hari ${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

function parseFlexibleDecimal(rawValue) {
  const raw = String(rawValue ?? "").trim();
  if (!/^\d+(?:[.,]\d+)?$/.test(raw)) return Number.NaN;
  return Number(raw.replace(",", "."));
}

function validateDecimalInput(input) {
  input.setCustomValidity("");
  const label = input.dataset.decimalLabel || "Nilai desimal";
  const value = parseFlexibleDecimal(input.value);
  if (!Number.isFinite(value)) {
    input.setCustomValidity(`${label} tidak valid. Gunakan satu koma atau titik, contoh 1,5 atau 1.5.`);
    return Number.NaN;
  }
  const minimum = Number(input.dataset.min);
  const maximum = Number(input.dataset.max);
  if (Number.isFinite(minimum) && value < minimum) {
    input.setCustomValidity(`${label} minimal ${input.dataset.min}.`);
    return Number.NaN;
  }
  if (Number.isFinite(maximum) && value > maximum) {
    input.setCustomValidity(`${label} maksimal ${input.dataset.max}.`);
    return Number.NaN;
  }
  return value;
}

function validateDecimalInputs(container, notify = false) {
  const inputs = Array.from(container.querySelectorAll("[data-decimal-input]"));
  const invalid = inputs.find((input) => !Number.isFinite(validateDecimalInput(input)));
  if (!invalid) return true;
  if (notify) {
    showToast(invalid.validationMessage, "error");
    invalid.reportValidity();
    invalid.focus();
  }
  return false;
}

function showToast(message, type = "success") {
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.className = `toast visible${type === "error" ? " error" : ""}`;
  toastTimer = setTimeout(() => { toast.className = "toast"; }, 3300);
}

function errorMessage(payload, fallback) {
  if (Array.isArray(payload?.detail)) return payload.detail.map((item) => item.msg).join(", ");
  return payload?.detail || fallback;
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    cache: "no-store",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let payload = null;
    try { payload = await response.json(); } catch (_) { /* keep status */ }
    const error = new Error(errorMessage(payload, `HTTP ${response.status}`));
    error.status = response.status;
    if (response.status === 401 && !path.startsWith("/api/auth/")) showAuth("login");
    throw error;
  }
  return response.json();
}

function scopedPath(path) {
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}ea_id=${encodeURIComponent(activeEaId)}`;
}

function userEaRecords(user = currentUser) {
  if (!user) return [];
  if (Array.isArray(user.eas) && user.eas.length) return user.eas;
  return user.ea_id ? [{
    ea_id: user.ea_id,
    allowed_ip: user.allowed_ip,
    api_token_hint: user.api_token_hint,
    api_token_active: user.api_token_active,
  }] : [];
}

function dashboardTargets() {
  if (currentUser?.role === "admin") {
    return adminUsers.flatMap((user) => userEaRecords(user).map((ea) => ({
      ...ea,
      username: user.username,
    })));
  }
  return userEaRecords().map((ea) => ({ ...ea, username: currentUser.username }));
}

function stopDashboardStream() {
  if (dashboardEventSource) dashboardEventSource.close();
  dashboardEventSource = null;
  lastDashboardStreamAt = 0;
}

function showAuth(view) {
  stopDashboardStream();
  currentUser = null;
  activeEaId = null;
  activeDashboardData = null;
  calculatorEaId = null;
  accountEaId = null;
  accountExpirationAtMs = null;
  accountExpirationUnlimited = false;
  accountExpirationRequired = false;
  accountExpirationEnabled = false;
  byId("appShell").hidden = true;
  byId("authShell").hidden = false;
  byId("setupView").hidden = view !== "setup";
  byId("loginView").hidden = view !== "login";
  byId("authError").hidden = true;
}

function setWorkspaceView(view) {
  const isAdmin = currentUser?.role === "admin";
  const showManagement = isAdmin && view === "users";
  const showAccount = Boolean(currentUser) && view === "account";
  const showCalculator = Boolean(currentUser) && view === "calculator";
  const showDashboard = Boolean(currentUser) && view === "dashboard";
  byId("dashboardView").hidden = !showDashboard;
  byId("adminManagementView").hidden = !showManagement;
  byId("accountSettingsView").hidden = !showAccount;
  byId("calculatorView").hidden = !showCalculator;
  byId("manageUsersButton").hidden = !isAdmin || !showDashboard;
  const eaCount = dashboardTargets().length;
  byId("targetPicker").hidden = !showDashboard || eaCount === 0;
  setText("targetPickerLabel", isAdmin ? "Kelola EA" : "EA Saya");
  byId("calculatorButton").hidden = !currentUser || showCalculator;
  byId("accountSettingsButton").hidden = !currentUser || showAccount;
  if (!showDashboard) stopDashboardStream();
}

function updateDashboardRoleVisibility() {
  const isAdmin = currentUser?.role === "admin";
  form.hidden = false;
  userTradingModeForm.hidden = isAdmin;
  byId("adminTradingModePanel").hidden = !isAdmin;
  byId("rsiSettingsFieldset").hidden = !isAdmin;
  byId("adminTradingModePanel").querySelectorAll("input, select, button").forEach((control) => {
    control.disabled = !isAdmin;
  });
  byId("rsiSettingsFieldset").querySelectorAll("input").forEach((control) => {
    control.disabled = !isAdmin;
  });
  byId("stopBehaviorPanel").hidden = false;
  scheduleForm.hidden = false;
}

async function openUserManagement() {
  if (currentUser?.role !== "admin") return;
  byId("newUserExpiresAt").min = jakartaDateTimeLocal(new Date(Date.now() + 60000).toISOString());
  setWorkspaceView("users");
  try {
    await loadUsers();
  } catch (error) {
    showToast(`Daftar user gagal dimuat: ${error.message}`, "error");
  }
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function returnToDashboard() {
  setWorkspaceView("dashboard");
  refreshDashboard();
  loadTradingModes();
  startDashboardStream();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function openCalculator() {
  await refreshDashboard();
  if (calculatorEaId !== activeEaId) useEaParameters(false);
  calculateEaRisk();
  setWorkspaceView("calculator");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function updateAccountView() {
  if (!currentUser) return;
  const ownEas = userEaRecords();
  if (!ownEas.some((ea) => ea.ea_id === accountEaId)) accountEaId = currentUser.ea_id;
  const selectedEa = ownEas.find((ea) => ea.ea_id === accountEaId) || ownEas[0] || {};
  const eaSelect = byId("accountEaSelect");
  eaSelect.replaceChildren(...ownEas.map((ea) => new Option(ea.ea_id, ea.ea_id)));
  eaSelect.value = selectedEa.ea_id || "";
  eaSelect.disabled = ownEas.length <= 1;
  setText("accountUsernameValue", currentUser.username);
  setText("accountRoleValue", currentUser.role === "admin" ? "Administrator" : "User");
  setText("accountEaIdValue", selectedEa.ea_id || "—");
  setText("accountAllowedIpValue", selectedEa.allowed_ip || "—");
  const tokenLabel = selectedEa.api_token_active
    ? `${"•".repeat(36)}${selectedEa.api_token_hint || ""}`
    : "Belum ada token aktif";
  setText("accountTokenValue", tokenLabel);
}

async function openAccountSettings() {
  try {
    currentUser = await request("/api/account");
    const ownIds = new Set(userEaRecords().map((ea) => ea.ea_id));
    accountEaId = ownIds.has(activeEaId) ? activeEaId : currentUser.ea_id;
    setText("userBadge", `${currentUser.username} · ${currentUser.role}`);
    updateAccountView();
    setWorkspaceView("account");
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (error) {
    showToast(`Pengaturan akun gagal dimuat: ${error.message}`, "error");
  }
}

function showAuthError(message) {
  setText("authError", message);
  byId("authError").hidden = false;
}

async function enterApp(user, token = null) {
  currentUser = user;
  activeEaId = user.ea_id;
  formDirty = false;
  scheduleDirty = false;
  stopModeDirty = false;
  tradingModeDirty = false;
  serverStopMode = "delete_pending";
  dashboardRefreshPromise = null;
  tradingModes = [];
  activeTradingModeName = null;
  activeDashboardData = null;
  calculatorEaId = null;
  accountEaId = user.ea_id;
  byId("authShell").hidden = true;
  byId("appShell").hidden = false;
  setText("userBadge", `${user.username} · ${user.role}`);
  const isAdmin = user.role === "admin";
  updateDashboardRoleVisibility();
  renderTradingModes([], null);
  setWorkspaceView("dashboard");
  if (isAdmin) await loadUsers();
  else renderTargetPicker();
  await refreshDashboard();
  await loadTradingModes();
  startDashboardStream();
  if (token) showToken(token, user.username, user.ea_id);
}

async function initializeSession() {
  try {
    const session = await request("/api/auth/session");
    if (session.setup_required) showAuth("setup");
    else if (session.authenticated) await enterApp(session.user);
    else showAuth("login");
  } catch (error) {
    showAuth("login");
    showAuthError(`Server tidak dapat dihubungi: ${error.message}`);
  }
}

async function submitSetup(event) {
  event.preventDefault();
  if (!event.currentTarget.reportValidity()) return;
  try {
    const data = await request("/api/auth/setup", {
      method: "POST",
      body: JSON.stringify({
        username: byId("setupUsername").value,
        password: byId("setupPassword").value,
        ea_id: byId("setupEaId").value,
        allowed_ip: byId("setupAllowedIp").value,
      }),
    });
    await enterApp(data.user, data.ea_token);
  } catch (error) {
    showAuthError(error.message);
  }
}

async function submitLogin(event) {
  event.preventDefault();
  if (!event.currentTarget.reportValidity()) return;
  try {
    const data = await request("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: byId("loginUsername").value,
        password: byId("loginPassword").value,
      }),
    });
    byId("loginPassword").value = "";
    await enterApp(data.user);
  } catch (error) {
    showAuthError(error.message);
  }
}

async function logout() {
  try { await request("/api/auth/logout", { method: "POST" }); } catch (_) { /* local logout */ }
  closeDeleteUserModal();
  showAuth("login");
}

function showToken(token, username, eaId = "") {
  setText("tokenModalTitle", `Token EA untuk ${username}${eaId ? ` · ${eaId}` : ""}`);
  setText("tokenValue", token);
  byId("tokenModal").hidden = false;
}

async function changeOwnPassword(event) {
  event.preventDefault();
  const submittedForm = event.currentTarget;
  if (!submittedForm.reportValidity()) return;
  const newPassword = byId("accountNewPassword").value;
  if (newPassword !== byId("accountConfirmNewPassword").value) {
    showToast("Ulangi password baru dengan nilai yang sama.", "error");
    return;
  }
  try {
    const data = await request("/api/account/change-password", {
      method: "POST",
      body: JSON.stringify({
        current_password: byId("currentPassword").value,
        new_password: newPassword,
      }),
    });
    currentUser = data.user;
    submittedForm.reset();
    updateAccountView();
    showToast("Password berhasil diganti. Sesi perangkat lain telah dikeluarkan.");
  } catch (error) {
    showToast(error.message, "error");
  }
}

async function rotateOwnToken(event) {
  event.preventDefault();
  const submittedForm = event.currentTarget;
  if (!submittedForm.reportValidity()) return;
  if (!window.confirm("Token lama akan langsung tidak berlaku dan EA harus memakai token baru. Lanjutkan?")) return;
  try {
    const targetEaId = accountEaId || currentUser.ea_id;
    const data = await request(`/api/account/rotate-token?ea_id=${encodeURIComponent(targetEaId)}`, {
      method: "POST",
      body: JSON.stringify({ current_password: byId("tokenCurrentPassword").value }),
    });
    currentUser = data.user;
    submittedForm.reset();
    updateAccountView();
    showToken(data.ea_token, currentUser.username, data.ea_id || targetEaId);
  } catch (error) {
    showToast(error.message, "error");
  }
}

function closeToken() {
  setText("tokenValue", "");
  byId("tokenModal").hidden = true;
}

function openDeleteUserModal(username) {
  pendingDeleteUsername = username;
  setText("deleteUserName", username);
  byId("deleteUserConfirmation").value = "";
  byId("confirmDeleteUserButton").disabled = true;
  byId("deleteUserModal").hidden = false;
  byId("deleteUserConfirmation").focus();
}

function closeDeleteUserModal() {
  pendingDeleteUsername = null;
  byId("deleteUserConfirmation").value = "";
  byId("confirmDeleteUserButton").disabled = true;
  byId("deleteUserModal").hidden = true;
}

function updateDeleteUserConfirmation() {
  const confirmation = byId("deleteUserConfirmation").value.trim().toLowerCase();
  byId("confirmDeleteUserButton").disabled = !pendingDeleteUsername || confirmation !== pendingDeleteUsername;
}

async function confirmDeleteUser(event) {
  event.preventDefault();
  const username = pendingDeleteUsername;
  if (!username || byId("deleteUserConfirmation").value.trim().toLowerCase() !== username) {
    showToast("Konfirmasi username tidak cocok. User tidak dihapus.", "error");
    return;
  }
  const deleteButton = byId("confirmDeleteUserButton");
  deleteButton.disabled = true;
  try {
    await request(`/api/admin/users/${encodeURIComponent(username)}`, { method: "DELETE" });
    closeDeleteUserModal();
    await loadUsers();
    showToast(`User ${username} berhasil dihapus.`);
  } catch (error) {
    updateDeleteUserConfirmation();
    showToast(error.message, "error");
  }
}

async function copyTextToClipboard(value) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch (_) { /* gunakan fallback untuk HTTP LAN */ }
  }
  const helper = document.createElement("textarea");
  helper.value = value;
  helper.setAttribute("readonly", "");
  helper.style.position = "fixed";
  helper.style.opacity = "0";
  document.body.append(helper);
  helper.select();
  let copied = false;
  try { copied = document.execCommand("copy"); } catch (_) { /* browser tidak mendukung */ }
  helper.remove();
  return copied;
}

async function copyEaId() {
  const eaId = byId("accountEaIdValue").textContent.trim();
  if (eaId && await copyTextToClipboard(eaId)) showToast("EA ID berhasil disalin.");
  else showToast("EA ID gagal disalin. Salin secara manual.", "error");
}

async function copyToken() {
  try {
    const copied = await copyTextToClipboard(byId("tokenValue").textContent);
    if (!copied) throw new Error("Clipboard tidak tersedia");
    showToast("Token berhasil disalin.");
  } catch (_) { showToast("Salin token secara manual dari kotak token.", "error"); }
}

function createButton(label, className, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${className}`;
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

function adminUserIdentitySignature(users) {
  return JSON.stringify(users.map((user) => ({
    username: user.username,
    role: user.role,
    enabled: user.enabled,
    expires_at: user.expires_at,
    expired: user.expired,
    expiration_required: user.expiration_required,
    disabled_reason: user.disabled_reason,
      eas: userEaRecords(user).map((ea) => ({
        ea_id: ea.ea_id,
        strategy_type: ea.strategy_type,
        allowed_ip: ea.allowed_ip,
      api_token_hint: ea.api_token_hint,
      api_token_active: ea.api_token_active,
    })),
  })));
}

function adminConnectionLabel(user) {
  const connection = user.connection;
  if (!connection?.last_seen) return "Belum ada heartbeat EA";
  const seen = new Date(connection.last_seen);
  const seenLabel = Number.isNaN(seen.getTime())
    ? "waktu tidak valid"
    : seen.toLocaleTimeString("id-ID");
  const parts = [];
  if (connection.account_login) parts.push(`MT5 ${connection.account_login}`);
  if (connection.symbol) parts.push(connection.symbol);
  if (connection.ea_version) parts.push(`EA v${connection.ea_version}`);
  if (connection.source_ip) parts.push(`IP ${connection.source_ip}`);
  parts.push(`${user.online ? "heartbeat" : "terakhir"} ${seenLabel}`);
  return parts.join(" · ");
}

function updateAdminFinancialRow(row, user) {
  const financial = user.financial || {};
  const currency = String(financial.account_currency || "USD").trim().toUpperCase() || "USD";
  const hasBalance = financial.account_balance !== null
    && financial.account_balance !== undefined
    && Number.isFinite(Number(financial.account_balance));
  const hasDailyProfit = financial.account_daily_profit !== null
    && financial.account_daily_profit !== undefined
    && Number.isFinite(Number(financial.account_daily_profit));

  const balance = row.querySelector(".admin-user-balance");
  balance.textContent = hasBalance ? accountMoney(financial.account_balance, currency) : "—";

  const dailyProfit = hasDailyProfit ? Number(financial.account_daily_profit) : 0;
  const profit = row.querySelector(".admin-user-daily-profit");
  profit.textContent = hasDailyProfit
    ? `${dailyProfit > 0 ? "+" : ""}${accountMoney(dailyProfit, currency)}`
    : "—";
  profit.className = `admin-user-daily-profit${dailyProfit > 0 ? " positive" : dailyProfit < 0 ? " negative" : ""}`;

  const note = row.querySelector(".admin-user-finance-note");
  if (hasBalance || hasDailyProfit) {
    const date = financial.account_profit_date || "tanggal belum tersedia";
    note.textContent = `${date} · ${user.online ? "heartbeat aktif" : "data terakhir"}`;
  } else {
    note.textContent = user.connection ? "Butuh EA V2.5.1+" : "Menunggu telemetry EA";
  }
}

function updateAdminExpirationRow(row, user) {
  const summary = row.querySelector(".admin-user-expiration-summary");
  if (!summary) return;
  if (user.role === "admin") {
    summary.textContent = "Tidak kedaluwarsa";
    summary.className = "admin-user-expiration-summary";
    return;
  }
  if (!user.expires_at) {
    summary.textContent = "Masa berlaku belum diatur";
    summary.className = "admin-user-expiration-summary expiration-warning";
    return;
  }
  const remaining = new Date(user.expires_at).getTime() - Date.now();
  summary.textContent = remaining <= 0 || user.expired
    ? `Kedaluwarsa · ${expirationDateLabel(user.expires_at)}`
    : `Berakhir ${expirationDateLabel(user.expires_at)} · sisa ${expirationRemainingLabel(remaining)}`;
  summary.className = `admin-user-expiration-summary${remaining <= 0 || user.expired ? " expiration-expired" : ""}`;
}

function updateAdminConnectionRow(row, user) {
  const role = row.querySelector(".admin-user-state");
  const accountState = user.expired ? "kedaluwarsa" : user.enabled ? "aktif" : "nonaktif";
  role.textContent = `${user.role} · ${accountState} · ${user.online ? "EA online" : "EA offline"}`;
  role.className = "admin-user-state";
  if (user.online) role.classList.add("online-label");
  if (!user.enabled) role.classList.add("disabled-label");

  const algorithm = row.querySelector(".admin-user-algorithm");
  const algorithmStatus = ["running", "stopped", "offline"].includes(user.algorithm_status)
    ? user.algorithm_status
    : "offline";
  algorithm.textContent = algorithmStatus === "running"
    ? "ALGO RUNNING"
    : algorithmStatus === "stopped" ? "ALGO STOP" : "ALGO OFFLINE";
  algorithm.className = `admin-user-algorithm algorithm-${algorithmStatus}`;

  const connection = row.querySelector(".admin-user-connection");
  connection.textContent = adminConnectionLabel(user);
  connection.title = user.connection?.client_id
    ? `Instance: ${user.connection.client_id}`
    : "Belum ada identitas instance EA";
  updateAdminFinancialRow(row, user);
  updateAdminExpirationRow(row, user);
}

function renderAdminUsers() {
  const list = byId("adminUserList");
  list.replaceChildren();
  const totalEas = adminUsers.reduce((total, user) => total + userEaRecords(user).length, 0);
  setText("userCountLabel", `${adminUsers.length} user · ${totalEas} EA`);
  for (const user of adminUsers) {
    const eas = userEaRecords(user);
    eas.forEach((eaRecord, eaIndex) => {
      const eaView = { ...user, ...eaRecord };
      const row = document.createElement("div");
      row.className = "admin-user-row";
      row.dataset.username = user.username;
      row.dataset.eaId = eaRecord.ea_id;

      const identity = document.createElement("div");
      identity.className = "admin-user-identity";
      const username = document.createElement("strong");
      username.textContent = user.username;
      const eaOrder = document.createElement("small");
      eaOrder.className = "admin-user-ea-order";
      eaOrder.textContent = eas.length > 1 ? `EA ${eaIndex + 1} dari ${eas.length}` : "1 EA terdaftar";
      const role = document.createElement("small");
      role.className = "admin-user-state";
      const algorithm = document.createElement("span");
      algorithm.className = "admin-user-algorithm";
      const connection = document.createElement("small");
      connection.className = "admin-user-connection";
      identity.append(username, eaOrder, role, algorithm, connection);

      const ea = document.createElement("div");
      ea.className = "admin-user-ea";
      const eaId = document.createElement("strong");
      eaId.textContent = eaRecord.ea_id;
      const tokenHint = document.createElement("small");
      tokenHint.textContent = `token …${eaRecord.api_token_hint || "belum ada"}`;
      const strategyBox = document.createElement("label");
      strategyBox.className = "admin-user-strategy";
      const strategyCaption = document.createElement("small");
      strategyCaption.textContent = "Jenis EA";
      const strategySelect = document.createElement("select");
      strategySelect.setAttribute("aria-label", `Jenis EA ${eaRecord.ea_id}`);
      for (const [value, label] of [["standard", "Regular"], ["trailing", "Trailing"]]) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        strategySelect.append(option);
      }
      const previousStrategy = eaRecord.strategy_type || "standard";
      strategySelect.value = previousStrategy;
      strategySelect.disabled = Boolean(eaRecord.online);
      strategySelect.title = eaRecord.online
        ? "EA sedang online. Hentikan atau lepas EA dari MT5 sebelum mengubah jenisnya."
        : "Jenis EA hanya dapat diubah administrator saat EA offline.";
      strategySelect.addEventListener("change", async () => {
        const nextStrategy = strategySelect.value;
        if (!window.confirm(`Ubah ${eaRecord.ea_id} menjadi ${strategyLabel(nextStrategy)}? EA ID dan token tetap sama. Pasang file EA yang sesuai di MT5.`)) {
          strategySelect.value = previousStrategy;
          return;
        }
        strategySelect.disabled = true;
        try {
          await request(`/api/admin/users/${encodeURIComponent(user.username)}/eas/${encodeURIComponent(eaRecord.ea_id)}/strategy`, {
            method: "PUT",
            body: JSON.stringify({ strategy_type: nextStrategy }),
          });
          await loadUsers();
          showToast(`${eaRecord.ea_id} sekarang memakai ${strategyLabel(nextStrategy)}. EA ID dan token tidak berubah.`);
        } catch (error) {
          strategySelect.value = previousStrategy;
          strategySelect.disabled = Boolean(eaRecord.online);
          showToast(error.message, "error");
        }
      });
      strategyBox.append(strategyCaption, strategySelect);
      ea.append(eaId, tokenHint, strategyBox);

      const financial = document.createElement("div");
      financial.className = "admin-user-finance";
      const balanceLabel = document.createElement("small");
      balanceLabel.textContent = "SALDO";
      const balance = document.createElement("strong");
      balance.className = "admin-user-balance";
      const profitLabel = document.createElement("small");
      profitLabel.textContent = "PROFIT HARI INI";
      const profit = document.createElement("strong");
      profit.className = "admin-user-daily-profit";
      const financeNote = document.createElement("small");
      financeNote.className = "admin-user-finance-note";
      financial.append(balanceLabel, balance, profitLabel, profit, financeNote);

      const ipBox = document.createElement("div");
      ipBox.className = "admin-user-ip";
      const ipInput = document.createElement("input");
      ipInput.value = eaRecord.allowed_ip || "*";
      ipInput.setAttribute("aria-label", `IP untuk ${eaRecord.ea_id}`);
      const saveIp = createButton("Simpan IP", "button-secondary", async () => {
        try {
          await request(`/api/admin/users/${encodeURIComponent(user.username)}/eas/${encodeURIComponent(eaRecord.ea_id)}`, {
            method: "PATCH",
            body: JSON.stringify({ allowed_ip: ipInput.value }),
          });
          await loadUsers();
          showToast(`IP ${eaRecord.ea_id} diperbarui.`);
        } catch (error) { showToast(error.message, "error"); }
      });
      ipBox.append(ipInput, saveIp);

      const expirationBox = document.createElement("div");
      expirationBox.className = "admin-user-expiration";
      const expirationSummary = document.createElement("small");
      expirationSummary.className = "admin-user-expiration-summary";
      expirationBox.append(expirationSummary);
      if (user.role !== "admin" && eaIndex === 0) {
        const expirationInput = document.createElement("input");
        expirationInput.type = "datetime-local";
        expirationInput.required = true;
        expirationInput.min = jakartaDateTimeLocal(new Date(Date.now() + 60000).toISOString());
        expirationInput.value = jakartaDateTimeLocal(user.expires_at);
        expirationInput.setAttribute("aria-label", `Masa berlaku ${user.username} zona Asia/Jakarta`);
        const saveExpiration = createButton("Simpan masa berlaku", "button-secondary", async () => {
          const expiresAt = jakartaLocalToIso(expirationInput.value);
          if (!expiresAt) {
            showToast("Tanggal dan waktu masa berlaku wajib diisi.", "error");
            expirationInput.focus();
            return;
          }
          try {
            await request(`/api/admin/users/${encodeURIComponent(user.username)}/expiration`, {
              method: "PUT",
              body: JSON.stringify({ expires_at: expiresAt }),
            });
            await loadUsers();
            showToast(`Masa berlaku ${user.username} diperbarui. Token EA tidak berubah.`);
          } catch (error) { showToast(error.message, "error"); }
        });
        expirationBox.append(expirationInput, saveExpiration);
      } else if (user.role !== "admin") {
        const followsAccount = document.createElement("small");
        followsAccount.textContent = `Mengikuti masa berlaku akun ${user.username}`;
        expirationBox.append(followsAccount);
      }

      const actions = document.createElement("div");
      actions.className = "admin-user-actions";
      actions.append(createButton("Buka dashboard", "button-secondary", async () => {
        await selectEa(eaRecord.ea_id);
        returnToDashboard();
      }));
      actions.append(createButton("Token baru", "button-secondary", async () => {
        if (!window.confirm(`Token lama ${eaRecord.ea_id} akan langsung tidak berlaku. Lanjutkan?`)) return;
        try {
          const data = await request(`/api/admin/users/${encodeURIComponent(user.username)}/eas/${encodeURIComponent(eaRecord.ea_id)}/rotate-token`, { method: "POST" });
          await loadUsers();
          if (data.user.username === currentUser.username) currentUser = data.user;
          showToken(data.ea_token, user.username, eaRecord.ea_id);
        } catch (error) { showToast(error.message, "error"); }
      }));
      if (eaIndex === 0 && user.role !== "admin") {
        actions.append(createButton(user.enabled ? "Nonaktifkan" : "Aktifkan", user.enabled ? "button-stop" : "button-primary", async () => {
          try {
            await request(`/api/admin/users/${encodeURIComponent(user.username)}`, {
              method: "PATCH",
              body: JSON.stringify({ enabled: !user.enabled }),
            });
            await loadUsers();
            showToast(`User ${user.username} ${user.enabled ? "dinonaktifkan" : "diaktifkan"}.`);
          } catch (error) { showToast(error.message, "error"); }
        }));
        actions.append(createButton("Hapus user", "button-stop", () => openDeleteUserModal(user.username)));
      }
      row.append(identity, ea, financial, ipBox, expirationBox, actions);
      updateAdminConnectionRow(row, eaView);
      list.append(row);
    });
  }
}

async function fetchAdminUsers() {
  if (adminUsersRefreshPromise) return adminUsersRefreshPromise;
  adminUsersRefreshPromise = request("/api/admin/users");
  try { return await adminUsersRefreshPromise; }
  finally { adminUsersRefreshPromise = null; }
}

function renderTargetPicker() {
  const select = byId("targetEaSelect");
  const targets = dashboardTargets();
  const availableIds = new Set(targets.map((target) => target.ea_id));
  if (!availableIds.has(activeEaId)) activeEaId = currentUser.ea_id;
  select.replaceChildren();
  for (const target of targets) {
    const option = document.createElement("option");
    option.value = target.ea_id;
    option.textContent = currentUser?.role === "admin"
      ? `${target.username} — ${target.ea_id}`
      : target.ea_id;
    option.selected = target.ea_id === activeEaId;
    select.append(option);
  }
  const showDashboard = !byId("dashboardView").hidden;
  byId("targetPicker").hidden = !showDashboard || targets.length === 0;
  setText("targetPickerLabel", currentUser?.role === "admin" ? "Kelola EA" : "EA Saya");
}

function renderAdditionalEaUserPicker() {
  const select = byId("additionalEaUsername");
  const previous = select.value;
  select.replaceChildren(...adminUsers.map((user) => new Option(user.username, user.username)));
  if (adminUsers.some((user) => user.username === previous)) select.value = previous;
}

async function loadUsers() {
  adminUsers = await fetchAdminUsers();
  renderTargetPicker();
  renderAdditionalEaUserPicker();
  renderAdminUsers();
}

async function refreshAdminUserConnections() {
  if (
    currentUser?.role !== "admin"
    || byId("adminManagementView").hidden
    || document.hidden
  ) return;
  try {
    const freshUsers = await fetchAdminUsers();
    const identityChanged = adminUserIdentitySignature(freshUsers) !== adminUserIdentitySignature(adminUsers);
    adminUsers = freshUsers;
    if (identityChanged) {
      renderTargetPicker();
      renderAdditionalEaUserPicker();
      renderAdminUsers();
      return;
    }
    const totalEas = adminUsers.reduce((total, user) => total + userEaRecords(user).length, 0);
    setText("userCountLabel", `${adminUsers.length} user · ${totalEas} EA`);
    const rows = [...byId("adminUserList").children];
    for (const user of adminUsers) {
      for (const ea of userEaRecords(user)) {
        const row = rows.find((candidate) => (
          candidate.dataset.username === user.username && candidate.dataset.eaId === ea.ea_id
        ));
        if (row) updateAdminConnectionRow(row, { ...user, ...ea });
      }
    }
  } catch (_) {
    // Dashboard utama tetap menangani notifikasi koneksi server.
  }
}

async function createUser(event) {
  event.preventDefault();
  const submittedForm = event.currentTarget;
  if (!submittedForm.reportValidity()) return;
  const expiresAt = jakartaLocalToIso(byId("newUserExpiresAt").value);
  if (!expiresAt) {
    showToast("Masa berlaku user wajib diisi.", "error");
    byId("newUserExpiresAt").focus();
    return;
  }
  try {
    const data = await request("/api/admin/users", {
      method: "POST",
      body: JSON.stringify({
        username: byId("newUsername").value,
        password: byId("newPassword").value,
        ea_id: byId("newEaId").value,
        allowed_ip: byId("newAllowedIp").value,
        expires_at: expiresAt,
      }),
    });
    submittedForm.reset();
    await loadUsers();
    showToken(data.ea_token, data.user.username, data.ea_id || data.user.ea_id);
  } catch (error) { showToast(error.message, "error"); }
}

async function addAdditionalEa(event) {
  event.preventDefault();
  const submittedForm = event.currentTarget;
  if (!submittedForm.reportValidity()) return;
  const username = byId("additionalEaUsername").value;
  try {
    const data = await request(`/api/admin/users/${encodeURIComponent(username)}/eas`, {
      method: "POST",
      body: JSON.stringify({
        ea_id: byId("additionalEaId").value,
        allowed_ip: byId("additionalEaAllowedIp").value,
      }),
    });
    const keepUsername = username;
    submittedForm.reset();
    byId("additionalEaAllowedIp").value = "*";
    await loadUsers();
    byId("additionalEaUsername").value = keepUsername;
    if (data.user.username === currentUser.username) currentUser = data.user;
    showToken(data.ea_token, data.user.username, data.ea_id);
  } catch (error) { showToast(error.message, "error"); }
}

async function selectEa(eaId) {
  stopDashboardStream();
  activeEaId = eaId;
  activeDashboardData = null;
  calculatorEaId = null;
  byId("targetEaSelect").value = eaId;
  formDirty = false;
  scheduleDirty = false;
  dailyProfitAutoStopDirty = false;
  stopModeDirty = false;
  tradingModeDirty = false;
  serverStopMode = "delete_pending";
  dashboardRefreshPromise = null;
  tradingModes = [];
  activeTradingModeName = null;
  renderTradingModes([], null);
  await refreshDashboard();
  await loadTradingModes();
  startDashboardStream();
  renderTargetPicker();
}

function setForm(config, force = false) {
  if (!force && (formDirty || form.matches(":focus-within"))) return;
  for (const [name, value] of Object.entries(config)) {
    const field = form.elements.namedItem(name);
    if (!field) continue;
    if (field.type === "checkbox") field.checked = Boolean(value);
    else field.value = value;
  }
}

function selectedAdminTradingMode() {
  const selectedName = byId("adminTradingModeSelect").value.toLowerCase();
  return tradingModes.find((mode) => mode.name.toLowerCase() === selectedName) || null;
}

function fillAdminTradingModeForm(mode = null) {
  const config = activeDashboardData?.config || {};
  byId("tradingModeName").value = mode?.name || "";
  byId("tradingModeRsiPeriod").value = mode?.rsi_period ?? config.rsi_period ?? 5;
  byId("tradingModeRsiBuy").value = mode?.rsi_buy_level ?? config.rsi_buy_level ?? 15;
  byId("tradingModeRsiSell").value = mode?.rsi_sell_level ?? config.rsi_sell_level ?? 95;
}

function updateTradingModeButtons() {
  const isAdmin = currentUser?.role === "admin";
  byId("deleteTradingModeButton").disabled = tradingModeBusy || !isAdmin || !selectedAdminTradingMode();
  byId("saveTradingModeButton").disabled = tradingModeBusy || !isAdmin;
  byId("activateTradingModeButton").disabled = tradingModeBusy || !byId("userTradingModeSelect").value;
}

function renderTradingModes(modes, activeMode = null, preferredName = "") {
  tradingModes = Array.isArray(modes) ? modes : [];
  activeTradingModeName = activeMode || null;

  const adminSelect = byId("adminTradingModeSelect");
  const previousAdminName = preferredName || adminSelect.value;
  adminSelect.replaceChildren(new Option("Profil baru…", ""));
  for (const mode of tradingModes) adminSelect.append(new Option(mode.name, mode.name));
  const adminMatch = tradingModes.find(
    (mode) => mode.name.toLowerCase() === String(previousAdminName).toLowerCase(),
  );
  adminSelect.value = adminMatch?.name || "";
  if (currentUser?.role === "admin") fillAdminTradingModeForm(adminMatch || null);
  setText("tradingModeCount", `${tradingModes.length} profil`);

  const userSelect = byId("userTradingModeSelect");
  const pendingUserName = tradingModeDirty ? userSelect.value : activeTradingModeName;
  userSelect.replaceChildren(new Option("Pilih profil…", ""));
  for (const mode of tradingModes) userSelect.append(new Option(mode.name, mode.name));
  const userMatch = tradingModes.find(
    (mode) => mode.name.toLowerCase() === String(pendingUserName || "").toLowerCase(),
  );
  userSelect.value = userMatch?.name || "";
  setText("activeTradingModeLabel", activeTradingModeName || "Belum dipilih");
  setText(
    "userTradingModeNote",
    tradingModes.length
      ? "Pilih nama profil yang disediakan administrator untuk EA akun Anda."
      : "Administrator belum membuat profil Entry Mode global.",
  );
  updateTradingModeButtons();
}

async function loadTradingModes(preferredName = "") {
  if (!currentUser || !activeEaId) return;
  const requestedEaId = activeEaId;
  try {
    const data = await request(scopedPath("/api/trading-modes"));
    if (activeEaId === requestedEaId) {
      renderTradingModes(data.modes, data.active_mode, preferredName);
    }
  } catch (error) {
    if (error.status !== 401) showToast(`Mode trading gagal dimuat: ${error.message}`, "error");
  }
}

function resetAdminTradingModeForm() {
  byId("adminTradingModeSelect").value = "";
  fillAdminTradingModeForm();
  updateTradingModeButtons();
}

async function saveTradingMode() {
  if (busy || tradingModeBusy || currentUser?.role !== "admin") return;
  const name = byId("tradingModeName").value.trim();
  if (!name) {
    showToast("Isi nama profil Entry Mode terlebih dahulu.", "error");
    byId("tradingModeName").focus();
    return;
  }
  const fields = [byId("tradingModeName"), byId("tradingModeRsiPeriod"), byId("tradingModeRsiBuy"), byId("tradingModeRsiSell")];
  const emptyRsiField = fields.slice(1).find((field) => !field.value.trim());
  if (emptyRsiField) {
    showToast("Periode RSI, Level beli, dan Level jual wajib diisi.", "error");
    emptyRsiField.focus();
    return;
  }
  const invalid = fields.find((field) => !field.reportValidity());
  if (invalid) return;
  tradingModeBusy = true;
  updateTradingModeButtons();
  try {
    const data = await request("/api/trading-modes", {
      method: "POST",
      body: JSON.stringify({
        name,
        rsi_period: Number(byId("tradingModeRsiPeriod").value),
        rsi_buy_level: Number(byId("tradingModeRsiBuy").value),
        rsi_sell_level: Number(byId("tradingModeRsiSell").value),
      }),
    });
    renderTradingModes(data.modes, activeTradingModeName, data.mode.name);
    showToast(data.created ? `Profil ${data.mode.name} berhasil dibuat.` : `Profil ${data.mode.name} berhasil diperbarui.`);
  } catch (error) { showToast(error.message, "error"); }
  finally {
    tradingModeBusy = false;
    updateTradingModeButtons();
  }
}

async function deleteSelectedTradingMode() {
  if (busy || tradingModeBusy || currentUser?.role !== "admin") return;
  const mode = selectedAdminTradingMode();
  if (!mode || !window.confirm(`Hapus profil ${mode.name}?`)) return;
  tradingModeBusy = true;
  updateTradingModeButtons();
  try {
    const path = `/api/trading-modes?mode_name=${encodeURIComponent(mode.name)}`;
    const data = await request(path, { method: "DELETE" });
    renderTradingModes(data.modes, activeTradingModeName);
    showToast(`Profil ${data.mode_name} berhasil dihapus.`);
  } catch (error) { showToast(error.message, "error"); }
  finally {
    tradingModeBusy = false;
    updateTradingModeButtons();
  }
}

async function activateTradingMode(event) {
  event.preventDefault();
  if (busy || tradingModeBusy || currentUser?.role !== "user") return;
  const name = byId("userTradingModeSelect").value;
  if (!name) {
    showToast("Pilih nama profil Entry Mode terlebih dahulu.", "error");
    byId("userTradingModeSelect").focus();
    return;
  }
  tradingModeBusy = true;
  updateTradingModeButtons();
  try {
    const data = await request(scopedPath("/api/trading-mode/active"), {
      method: "PUT",
      body: JSON.stringify({ name }),
    });
    tradingModeDirty = false;
    render(data);
    await loadTradingModes();
    showToast(`Profil ${name} diterapkan ke EA akun Anda.`);
  } catch (error) { showToast(error.message, "error"); }
  finally {
    tradingModeBusy = false;
    updateTradingModeButtons();
  }
}

function calculatorNumber(id, fallback, minimum = 0, maximum = Number.MAX_SAFE_INTEGER) {
  const input = byId(id);
  const value = input.matches("[data-decimal-input]")
    ? parseFlexibleDecimal(input.value)
    : Number(input.value);
  if (!Number.isFinite(value)) return fallback;
  return Math.min(maximum, Math.max(minimum, value));
}

function calculatorNumberLabel(value, maximumFractionDigits = 2) {
  const normalized = Math.abs(value) < 1e-10 ? 0 : value;
  return new Intl.NumberFormat("id-ID", { maximumFractionDigits }).format(normalized);
}

function normalizedCalculatorLot(volume, maximumLot, roundUp = false) {
  const step = 0.01;
  const capped = Math.min(volume, maximumLot);
  const normalizedCap = Math.floor(maximumLot / step + 1e-8) * step;
  const units = roundUp
    ? Math.ceil(capped / step - 1e-8)
    : Math.floor(capped / step + 1e-8);
  const normalized = Math.min(units * step, normalizedCap);
  return Math.round(Math.max(step, normalized) * 100) / 100;
}

function calculatorScenarioMaximum(maximumMartingale, martingaleEnabled = true) {
  const parsed = Number(maximumMartingale);
  const normalizedStages = Number.isFinite(parsed)
    ? Math.floor(Math.min(Number.MAX_SAFE_INTEGER - 1, Math.max(0, parsed)))
    : Number.MAX_SAFE_INTEGER - 1;
  const requestedPositions = martingaleEnabled ? normalizedStages + 1 : 1;
  const limited = requestedPositions > MAX_CALCULATOR_SCENARIO_POSITIONS;
  const limitNote = byId("calculatorLimitNote");
  limitNote.hidden = !limited;
  if (limited) {
    limitNote.textContent = `EA tetap menyimpan ${calculatorNumberLabel(normalizedStages, 0)} tahap. Kalkulator membatasi simulasi dan tabel pada ${calculatorNumberLabel(MAX_CALCULATOR_SCENARIO_POSITIONS, 0)} posisi agar browser tetap responsif.`;
  }
  return Math.min(requestedPositions, MAX_CALCULATOR_SCENARIO_POSITIONS);
}

function setCalculatorValues(values, selectMaximumPositions = true) {
  byId("calcMartingaleEnabled").checked = Boolean(values.martingale_enabled);
  byId("calcInitialLot").value = values.initial_lot;
  byId("calcLotMultiplier").value = values.lot_multiplier;
  byId("calcDistancePips").value = values.martingale_distance_pips;
  byId("calcMaxMartingale").value = values.max_martingale;
  byId("calcMaxLot").value = values.max_lot;
  byId("calcPipValueUsd").value = values.pip_value_usd;
  byId("calcTpPips").value = values.tp_pips_from_bep;
  byId("calcSlPips").value = values.sl_pips_from_last;
  const maximumPositions = calculatorScenarioMaximum(values.max_martingale, Boolean(values.martingale_enabled));
  byId("calcPositionRange").max = Math.max(1, maximumPositions);
  if (selectMaximumPositions) byId("calcPositionRange").value = Math.max(1, maximumPositions);
  calculateEaRisk();
}

function resetCalculator() {
  setCalculatorValues(CALCULATOR_DEFAULTS);
  calculatorEaId = null;
}

function useEaParameters(showMessage = true) {
  const config = activeDashboardData?.config;
  if (!config) {
    if (showMessage) showToast("Parameter EA belum tersedia.", "error");
    return;
  }
  setCalculatorValues({
    martingale_enabled: config.martingale_enabled,
    initial_lot: config.initial_lot,
    lot_multiplier: config.lot_multiplier,
    martingale_distance_pips: config.martingale_distance_pips,
    max_martingale: config.max_martingale,
    max_lot: config.max_lot,
    pip_value_usd: calculatorNumber("calcPipValueUsd", CALCULATOR_DEFAULTS.pip_value_usd, 0.01),
    tp_pips_from_bep: config.tp_pips_from_bep ?? config.trailing_start_pips ?? CALCULATOR_DEFAULTS.tp_pips_from_bep,
    sl_pips_from_last: config.sl_pips_from_last ?? config.initial_sl_pips ?? CALCULATOR_DEFAULTS.sl_pips_from_last,
  });
  calculatorEaId = activeEaId;
  if (showMessage) showToast("Parameter EA dimuat ke kalkulator.");
}

function calculateEaRisk() {
  if (!validateDecimalInputs(calculatorForm)) return;
  const martingaleEnabled = byId("calcMartingaleEnabled").checked;
  const initialLot = calculatorNumber("calcInitialLot", 0.01, 0.01, 100);
  const multiplier = calculatorNumber("calcLotMultiplier", 1, 1, 10);
  const distancePips = calculatorNumber("calcDistancePips", 1, 1, 100000);
  const maximumMartingale = Math.floor(calculatorNumber("calcMaxMartingale", 0, 0));
  const maximumLot = calculatorNumber("calcMaxLot", initialLot, 0.01, 100);
  const pipValueUsd = calculatorNumber("calcPipValueUsd", 10, 0.01, 100000);
  const takeProfitPips = calculatorNumber("calcTpPips", 1, 1, 100000);
  const stopLossPips = calculatorNumber("calcSlPips", 1, 1, 100000);
  const positionRange = byId("calcPositionRange");
  const maximumPositions = calculatorScenarioMaximum(maximumMartingale, martingaleEnabled);
  positionRange.max = maximumPositions;
  positionRange.disabled = !martingaleEnabled;
  const requestedPositions = Math.floor(calculatorNumber("calcPositionRange", maximumPositions, 1, maximumPositions));
  const positionCount = martingaleEnabled ? requestedPositions : 1;
  positionRange.value = positionCount;
  setText("calcPositionCount", `${positionCount} posisi`);

  const rows = [];
  for (let index = 0; index < positionCount; index += 1) {
    rows.push({
      lot: normalizedCalculatorLot(initialLot * (multiplier ** index), maximumLot, index > 0),
      entry: -index * distancePips,
    });
  }
  const totalLot = rows.reduce((sum, row) => sum + row.lot, 0);
  const bep = totalLot > 0 ? rows.reduce((sum, row) => sum + row.entry * row.lot, 0) / totalLot : 0;
  const takeProfitRelative = bep + takeProfitPips;
  const lowestLayerEntry = rows[rows.length - 1].entry;
  const takeProfitFromLowest = takeProfitRelative - lowestLayerEntry;
  const stopLossRelative = lowestLayerEntry - stopLossPips;
  for (const row of rows) {
    row.takeProfitValue = (takeProfitRelative - row.entry) * row.lot * pipValueUsd;
    row.stopLossValue = (stopLossRelative - row.entry) * row.lot * pipValueUsd;
  }
  const profit = rows.reduce((sum, row) => sum + row.takeProfitValue, 0);
  const loss = rows.reduce((sum, row) => sum + row.stopLossValue, 0);
  const rewardRatio = loss === 0 ? 0 : profit / Math.abs(loss);

  setText("calcProfitValue", money(profit));
  setText("calcLossValue", money(loss));
  setText("calcTpInfo", `Exit BEP + ${calculatorNumberLabel(takeProfitPips)} pip`);
  setText("calcSlInfo", `SL ${calculatorNumberLabel(stopLossPips)} pip dari posisi terakhir`);
  setText("calcRiskReward", `1 : ${calculatorNumberLabel(rewardRatio)}`);
  setText("calcTotalLot", calculatorNumberLabel(totalLot));
  setText("calcBepRelative", `${calculatorNumberLabel(bep)} pip`);
  setText("calcTpRelative", `${calculatorNumberLabel(takeProfitRelative)} pip`);
  setText("calcTpFromLowest", `${calculatorNumberLabel(takeProfitFromLowest)} pip`);
  setText("calcSlRelative", `${calculatorNumberLabel(stopLossRelative)} pip`);
  byId("calcRows").innerHTML = rows.map((row, index) => (
    `<tr><td>${index + 1}</td><td>${row.lot.toFixed(2)}</td><td>${calculatorNumberLabel(row.entry)} pip</td>`
    + `<td class="calculator-positive">${money(row.takeProfitValue)}</td>`
    + `<td class="${row.stopLossValue >= 0 ? "calculator-positive" : "calculator-negative"}">${money(row.stopLossValue)}</td></tr>`
  )).join("");
}

function money(value) {
  return accountMoney(value, calculatorCurrency);
}

function accountMoney(value, currency) {
  const amount = Number(value);
  const code = String(currency || "USD").trim().toUpperCase() || "USD";
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: code,
      minimumFractionDigits: 2,
    }).format(amount);
  } catch (_) {
    return `${code} ${new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(amount)}`;
  }
}

function setCalculatorCurrency(currency) {
  const code = String(currency || "USD").trim().toUpperCase() || "USD";
  const changed = calculatorCurrency !== code;
  calculatorCurrency = code;
  setText("calcPipCurrency", code);
  setText("calculatorCurrencyNote", `Estimasi ditampilkan dalam ${code}. Isi nilai pip sesuai broker; desimal menerima koma atau titik.`);
  if (changed) calculateEaRisk();
}

function updateScheduleCountdown() {
  if (!scheduleNextAtMs || !scheduleNextAction) {
    setText("scheduleCountdown", "Jadwal nonaktif");
    return;
  }
  const remaining = Math.max(0, scheduleNextAtMs - (Date.now() + scheduleClockOffsetMs));
  const totalSeconds = Math.floor(remaining / 1000);
  const hours = String(Math.floor(totalSeconds / 3600)).padStart(2, "0");
  const minutes = String(Math.floor((totalSeconds % 3600) / 60)).padStart(2, "0");
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  const action = scheduleNextAction === "start" ? "START" : "STOP";
  setText("scheduleCountdown", remaining > 0 ? `${action} dalam ${hours}:${minutes}:${seconds}` : `Menerapkan ${action}…`);
}

function updateAccountExpirationCountdown() {
  const value = byId("accountExpirationValue");
  if (accountExpirationUnlimited) {
    value.textContent = "Tidak terbatas";
    value.className = "metric-main account-expiration-value positive";
    setText("accountExpirationNote", "Akun administrator tidak memiliki masa kedaluwarsa");
    return;
  }
  if (accountExpirationRequired || !Number.isFinite(accountExpirationAtMs)) {
    value.textContent = "Belum diatur";
    value.className = "metric-main account-expiration-value negative";
    setText("accountExpirationNote", "Administrator harus menetapkan masa berlaku akun");
    return;
  }
  const remaining = accountExpirationAtMs - Date.now();
  value.textContent = expirationDateLabel(new Date(accountExpirationAtMs).toISOString());
  const expired = remaining <= 0;
  value.className = `metric-main account-expiration-value${expired ? " negative" : ""}`;
  setText(
    "accountExpirationNote",
    expired
      ? "AKUN KEDALUWARSA"
      : `${accountExpirationEnabled ? "Aktif" : "Nonaktif"} · sisa ${expirationRemainingLabel(remaining)} · Asia/Jakarta`,
  );
}

function setAccountExpiration(expiration = {}) {
  accountExpirationUnlimited = Boolean(expiration.unlimited);
  accountExpirationRequired = Boolean(expiration.expiration_required);
  accountExpirationEnabled = Boolean(expiration.enabled);
  const parsed = Date.parse(expiration.expires_at || "");
  accountExpirationAtMs = Number.isFinite(parsed) ? parsed : null;
  updateAccountExpirationCountdown();
}

function updateStopModeHint() {
  const hints = {
    pause_only: "Entry awal baru dihentikan; posisi aktif, martingale, dan proteksi siklus tetap dikelola.",
    delete_pending: "Pending order milik EA dihapus; entry awal baru berhenti dan siklus aktif tetap dikelola.",
    close_all: "Pending dan seluruh posisi dengan simbol serta MagicNumber EA akan ditutup.",
  };
  const pending = stopModeDirty ? " Pilihan belum diterapkan—tekan tombol Stop untuk mengirimkannya." : "";
  setText("stopHint", `${hints[stopMode.value] || ""}${pending}`);
}

function setSchedule(data) {
  const schedule = data.schedule || {};
  if (!scheduleDirty && !scheduleForm.matches(":focus-within")) {
    byId("scheduleEnabled").checked = Boolean(schedule.enabled);
    byId("scheduleStartTime").value = schedule.start_time || "06:00";
    byId("scheduleStopTime").value = schedule.stop_time || "23:00";
  }
  const labels = { disabled: "Nonaktif", running: "Entry aktif", stopped: "Entry berhenti" };
  const state = data.schedule_phase || "disabled";
  byId("scheduleState").dataset.state = state;
  setText("scheduleState", labels[state] || state);
  setText("scheduleTimezone", schedule.timezone || "Asia/Jakarta");
  const serverTime = Date.parse(data.schedule_server_time || "");
  scheduleClockOffsetMs = Number.isFinite(serverTime) ? serverTime - Date.now() : 0;
  const nextAt = Date.parse(data.schedule_next_at || "");
  scheduleNextAtMs = Number.isFinite(nextAt) ? nextAt : null;
  scheduleNextAction = data.schedule_next_action || null;
  updateScheduleCountdown();
}

function render(data) {
  activeDashboardData = {
      ea_id: data.ea_id,
      owner: data.owner,
      strategy_type: data.strategy_type || "standard",
      config: { ...data.config },
      algo_enabled: data.algo_enabled,
  };
  setText("calculatorContext", `${data.owner || "—"} · ${data.ea_id}`);
  const status = data.status || {};
  setAccountExpiration(data.account_expiration || {});
  const connectionPill = byId("connectionPill");
  connectionPill.dataset.state = data.online ? "online" : "offline";
  setText("connectionText", data.online ? "EA Online" : "Menunggu EA");
  const algoLabel = !data.online ? "OFFLINE" : data.algo_enabled ? "RUNNING" : "STOPPED";
  const algoColor = !data.online ? "var(--muted)" : data.algo_enabled ? "var(--green)" : "var(--red)";
  setText("algoStatus", algoLabel);
  byId("algoStatus").style.color = algoColor;
  setText("symbolValue", status.symbol || "XAUUSD");
  setText("bidValue", status.bid ? Number(status.bid).toFixed(3) : "—");
  setText("askValue", status.ask ? Number(status.ask).toFixed(3) : "—");
  const rsi = Math.max(0, Math.min(100, Number(status.rsi || 0)));
  const hasRsi = data.online
    && status.rsi !== null
    && status.rsi !== undefined
    && Number.isFinite(Number(status.rsi));
  const buyZoneEnd = Math.max(0, Math.min(100, Number(data.rsi_indicator?.buy_zone_end ?? 15)));
  const sellZoneStart = Math.max(buyZoneEnd, Math.min(100, Number(data.rsi_indicator?.sell_zone_start ?? 95)));
  const rsiSignal = !hasRsi ? "waiting" : rsi <= buyZoneEnd ? "buy" : rsi >= sellZoneStart ? "sell" : "neutral";
  const rsiSignalLabels = { waiting: "Menunggu RSI EA", buy: "AREA BUY", neutral: "NETRAL — MENUNGGU SINYAL", sell: "AREA SELL" };
  setText("rsiValue", hasRsi ? rsi.toFixed(1) : "—");
  byId("rsiValue").style.color = rsiSignal === "buy" ? "var(--green)" : rsiSignal === "sell" ? "var(--red)" : "var(--text)";
  byId("rsiNeedle").style.left = `${hasRsi ? rsi : 50}%`;
  byId("rsiTrack").style.background = `linear-gradient(90deg, var(--green) 0 ${buyZoneEnd}%, #3a4b44 ${buyZoneEnd}% ${sellZoneStart}%, var(--red) ${sellZoneStart}% 100%)`;
  byId("rsiTrack").setAttribute("aria-label", `${rsiSignalLabels[rsiSignal]}${hasRsi ? `, RSI ${rsi.toFixed(1)}` : ""}`);
  byId("rsiSignalText").dataset.signal = rsiSignal;
  setText("rsiSignalText", rsiSignalLabels[rsiSignal]);
  const liveRsiTelemetry = Number.parseFloat(status.ea_version || "0") >= 2.54;
  setText(
    "rsiSourceNote",
    !data.online
      ? "Menunggu telemetry EA"
      : liveRsiTelemetry
        ? "RSI live · sinyal entry tetap memakai candle selesai"
        : "RSI candle selesai · gunakan EA V2.5.4 untuk indikator live",
  );
  const currency = status.account_currency || "USD";
  setCalculatorCurrency(currency);
  const hasAccountFloatingProfit = status.account_floating_profit !== null
    && status.account_floating_profit !== undefined
    && Number.isFinite(Number(status.account_floating_profit));
  const profit = hasAccountFloatingProfit ? Number(status.account_floating_profit) : 0;
  setText("profitValue", hasAccountFloatingProfit ? accountMoney(profit, currency) : "—");
  byId("profitValue").className = `metric-main${profit > 0 ? " positive" : profit < 0 ? " negative" : ""}`;
  setText(
    "floatingProfitNote",
    hasAccountFloatingProfit
      ? `${currency} · seluruh posisi akun saat heartbeat terakhir`
      : data.online ? "Butuh EA V2.5.1" : "Menunggu telemetry EA",
  );
  const maximumLayers = Math.max(1, Math.floor(Number(data.config?.max_martingale || 0)) + 1);
  setText("buyPositions", `${status.buy_positions ?? 0}/${maximumLayers}`);
  setText("sellPositions", `${status.sell_positions ?? 0}/${maximumLayers}`);
  const hasBalance = status.account_balance !== null
      && status.account_balance !== undefined
      && Number.isFinite(Number(status.account_balance));
  const balance = hasBalance ? Number(status.account_balance) : null;
  const balanceZero = hasBalance && balance === 0;
  setText("accountBalanceValue", hasBalance ? accountMoney(status.account_balance, currency) : "—");
  setText(
      "accountBalanceNote",
      hasBalance
          ? (balanceZero ? "Saldo 0 — EA tidak dapat berjalan" : `${currency} · saldo akun saat heartbeat terakhir`)
          : data.online ? "Butuh EA V2.5.0" : "Menunggu telemetry EA",
  );
  // Disable start button when balance is 0
  const startButton = byId("startButton");
  if (balanceZero) {
      startButton.disabled = true;
      startButton.title = "Saldo akun 0 — EA tidak dapat dimulai";
  } else {
      startButton.disabled = false;
      startButton.title = "";
  }
  if (data.auto_stopped_due_to_balance) {
      showToast("EA dihentikan otomatis karena saldo akun 0.", "error");
  }
  if (data.daily_profit_auto_stopped_now) {
      showToast("Entry baru dihentikan karena target profit harian tercapai. Posisi aktif tetap dikelola.", "error");
  }

  const hasDailyProfit = status.account_daily_profit !== null
    && status.account_daily_profit !== undefined
    && Number.isFinite(Number(status.account_daily_profit));
  const dailyProfit = hasDailyProfit ? Number(status.account_daily_profit) : 0;
  setText("dailyProfitValue", hasDailyProfit ? accountMoney(dailyProfit, currency) : "—");
  byId("dailyProfitValue").className = `metric-main${dailyProfit > 0 ? " positive" : dailyProfit < 0 ? " negative" : ""}`;
  setText(
    "dailyProfitDate",
    hasDailyProfit
      ? `Profit trading net ${status.account_profit_date || "hari ini"} · waktu server broker`
      : data.online ? "Butuh EA V2.5.1" : "Profit trading net · waktu server broker",
  );
  const dailyStopEnabled = Boolean(data.daily_profit_auto_stop_enabled);
  const dailyStopTarget = Number(data.daily_profit_target || 0);
  if (!dailyProfitAutoStopDirty) {
    byId("dailyProfitAutoStopEnabled").checked = dailyStopEnabled;
    byId("dailyProfitTarget").value = dailyStopTarget > 0 ? dailyStopTarget : "";
  }
  setText("dailyProfitAutoStopNote", data.daily_profit_auto_stop_triggered
    ? "Target tercapai: entry baru dihentikan. Posisi aktif tetap dikelola."
    : dailyStopEnabled ? `Target aktif: ${accountMoney(dailyStopTarget, currency)}` : "Auto-stop tidak aktif.");
  setText("totalPositions", status.total_positions ?? 0);
  setText("accountValue", status.account_login || "—");
  setText("lastSeenValue", status.last_seen ? new Date(status.last_seen).toLocaleTimeString("id-ID") : "Belum ada");
  setText("revisionLabel", `rev ${data.config_revision}`);
  setText("ownerValue", data.owner || "—");
  setText("eaIdValue", data.ea_id);
  setText("allowedIpValue", data.allowed_ip || "—");
  serverStopMode = data.stop_mode || "delete_pending";
  if (!stopModeDirty) stopMode.value = serverStopMode;
  else if (stopMode.value === serverStopMode) stopModeDirty = false;
  updateStopModeHint();
  activeTradingModeName = data.active_trading_mode || null;
  setStrategyFields(data.strategy_type || "standard");
  setForm(data.config);
  setSchedule(data);
  if (currentUser?.role !== "admin" && !tradingModeDirty) {
    const matchingMode = tradingModes.find(
      (mode) => mode.name.toLowerCase() === String(activeTradingModeName || "").toLowerCase(),
    );
    byId("userTradingModeSelect").value = matchingMode?.name || "";
    setText("activeTradingModeLabel", activeTradingModeName || "Belum dipilih");
    updateTradingModeButtons();
  }
  const errorBox = byId("errorBox");
  errorBox.hidden = !status.last_error;
  errorBox.textContent = status.last_error || "";
}

function collectConfig(refreshProtection) {
  const output = { refresh_protection: refreshProtection };
  for (const [name, field] of Object.entries(Object.fromEntries(new FormData(form)))) {
    output[name] = decimalConfigFields.has(name)
      ? parseFlexibleDecimal(field)
      : numberFields.has(name) ? Number(field) : field;
  }
  output.martingale_enabled = form.elements.martingale_enabled.checked;
  return output;
}

async function saveConfig(refreshProtection = false) {
  if (busy) return;
  if (!validateDecimalInputs(form, true) || !form.reportValidity()) return;
  busy = true;
  const configButtons = [byId("saveButton"), byId("refreshProtectionButton")];
  configButtons.forEach((button) => { button.disabled = true; });
  try {
    const data = await request(scopedPath("/api/config"), {
      method: "PUT",
      body: JSON.stringify(collectConfig(refreshProtection)),
    });
    formDirty = false;
    render(data);
    showToast(refreshProtection ? "Parameter tersimpan; refresh SL/TP dikirim ke EA." : "Parameter runtime berhasil disimpan.");
  } catch (error) { showToast(error.message, "error"); }
  finally {
    busy = false;
    configButtons.forEach((button) => { button.disabled = false; });
  }
}

async function saveDailyProfitAutoStop() {
  const enabled = byId("dailyProfitAutoStopEnabled").checked;
  const rawTarget = byId("dailyProfitTarget").value.replace(",", ".").trim();
  const target = rawTarget === "" ? null : Number(rawTarget);
  if (enabled && (!Number.isFinite(target) || target <= 0)) {
    showToast("Target profit harian harus lebih besar dari 0.", "error");
    byId("dailyProfitTarget").focus();
    return;
  }
  const button = byId("saveDailyProfitAutoStopButton");
  button.disabled = true;
  try {
    const data = await request(scopedPath("/api/daily-profit-auto-stop"), {
      method: "PATCH",
      body: JSON.stringify({ enabled, target }),
    });
    dailyProfitAutoStopDirty = false;
    render(data);
    showToast(enabled ? "Auto-stop profit harian berhasil diaktifkan." : "Auto-stop profit harian dimatikan.");
  } catch (error) { showToast(error.message, "error"); }
  finally { button.disabled = false; }
}

async function setControl(action) {
  if (busy) return;
  if (action === "stop" && stopMode.value === "close_all" && !window.confirm("Mode Close All akan menutup semua posisi milik EA ini. Lanjutkan?")) return;
  const requestedStopMode = stopMode.value;
  busy = true;
  try {
    const data = await request(scopedPath("/api/control"), {
      method: "POST",
      body: JSON.stringify({ action, stop_mode: requestedStopMode }),
    });
    stopModeDirty = false;
    render(data);
    showToast(action === "start" ? "Perintah START dikirim." : "Perintah STOP dikirim.");
  } catch (error) { showToast(error.message, "error"); }
  finally { busy = false; }
}

async function saveSchedule() {
  if (busy || !scheduleForm.reportValidity()) return;
  if (byId("scheduleStartTime").value === byId("scheduleStopTime").value) {
    showToast("Waktu START dan STOP tidak boleh sama.", "error");
    return;
  }
  busy = true;
  const scheduleButton = byId("saveScheduleButton");
  scheduleButton.disabled = true;
  try {
    const data = await request(scopedPath("/api/schedule"), {
      method: "PUT",
      body: JSON.stringify({
        enabled: byId("scheduleEnabled").checked,
        start_time: byId("scheduleStartTime").value,
        stop_time: byId("scheduleStopTime").value,
      }),
    });
    scheduleDirty = false;
    render(data);
    showToast("Jadwal otomatis berhasil disimpan.");
  } catch (error) { showToast(error.message, "error"); }
  finally {
    busy = false;
    scheduleButton.disabled = false;
  }
}

async function refreshDashboard() {
  if (!currentUser || !activeEaId) return;
  if (dashboardRefreshPromise) return dashboardRefreshPromise;
  const requestedEaId = activeEaId;
  dashboardRefreshPromise = (async () => {
    try {
      const data = await request(`/api/dashboard?ea_id=${encodeURIComponent(requestedEaId)}`);
      if (activeEaId === requestedEaId) render(data);
    } catch (error) {
      if (error.status !== 401) {
        byId("connectionPill").dataset.state = "offline";
        setText("connectionText", "Server terputus");
      }
    }
  })();
  try { await dashboardRefreshPromise; }
  finally { dashboardRefreshPromise = null; }
}

function startDashboardStream() {
  stopDashboardStream();
  if (!currentUser || !activeEaId || typeof EventSource === "undefined") return;
  const requestedEaId = activeEaId;
  const stream = new EventSource(`/api/dashboard/stream?ea_id=${encodeURIComponent(requestedEaId)}`);
  dashboardEventSource = stream;
  stream.onmessage = (event) => {
    if (dashboardEventSource !== stream || activeEaId !== requestedEaId) return;
    try {
      const data = JSON.parse(event.data);
      lastDashboardStreamAt = Date.now();
      render(data);
    } catch (_) {
      // Paket SSE yang tidak lengkap diabaikan; polling cadangan tetap aktif.
    }
  };
  stream.onerror = () => {
    // EventSource mencoba tersambung ulang otomatis; polling menjadi jalur cadangan.
  };
}

function refreshDashboardFallback() {
  if (document.hidden || !currentUser || !activeEaId) return;
  if (dashboardEventSource && Date.now() - lastDashboardStreamAt < 1500) return;
  refreshDashboard();
}

byId("setupForm").addEventListener("submit", submitSetup);
byId("loginForm").addEventListener("submit", submitLogin);
byId("logoutButton").addEventListener("click", logout);
byId("manageUsersButton").addEventListener("click", openUserManagement);
byId("backToDashboardButton").addEventListener("click", returnToDashboard);
byId("calculatorButton").addEventListener("click", openCalculator);
byId("backFromCalculatorButton").addEventListener("click", returnToDashboard);
byId("accountSettingsButton").addEventListener("click", openAccountSettings);
byId("backFromAccountButton").addEventListener("click", returnToDashboard);
byId("changePasswordForm").addEventListener("submit", changeOwnPassword);
byId("rotateOwnTokenForm").addEventListener("submit", rotateOwnToken);
byId("createUserForm").addEventListener("submit", createUser);
byId("addEaForm").addEventListener("submit", addAdditionalEa);
byId("targetEaSelect").addEventListener("change", (event) => selectEa(event.target.value));
byId("accountEaSelect").addEventListener("change", (event) => {
  accountEaId = event.target.value;
  updateAccountView();
});
byId("copyEaIdButton").addEventListener("click", copyEaId);
byId("copyTokenButton").addEventListener("click", copyToken);
byId("closeTokenButton").addEventListener("click", closeToken);
byId("cancelDeleteUserButton").addEventListener("click", closeDeleteUserModal);
byId("deleteUserConfirmation").addEventListener("input", updateDeleteUserConfirmation);
byId("deleteUserConfirmForm").addEventListener("submit", confirmDeleteUser);
byId("adminTradingModeSelect").addEventListener("change", () => {
  fillAdminTradingModeForm(selectedAdminTradingMode());
  updateTradingModeButtons();
});
byId("newTradingModeButton").addEventListener("click", resetAdminTradingModeForm);
byId("saveTradingModeButton").addEventListener("click", saveTradingMode);
byId("deleteTradingModeButton").addEventListener("click", deleteSelectedTradingMode);
byId("activateTradingModeButton").addEventListener("click", activateTradingMode);
byId("userTradingModeSelect").addEventListener("change", () => {
  tradingModeDirty = byId("userTradingModeSelect").value !== activeTradingModeName;
  updateTradingModeButtons();
});
calculatorForm.addEventListener("input", calculateEaRisk);
document.querySelectorAll("[data-decimal-input]").forEach((input) => {
  input.addEventListener("input", () => validateDecimalInput(input));
  input.addEventListener("blur", () => {
    if (Number.isFinite(validateDecimalInput(input))) return;
    showToast(input.validationMessage, "error");
    input.reportValidity();
  });
});
byId("calcMartingaleEnabled").addEventListener("change", () => {
  byId("calcPositionRange").value = byId("calcMartingaleEnabled").checked
    ? calculatorScenarioMaximum(calculatorNumber("calcMaxMartingale", 0, 0), true)
    : 1;
  calculateEaRisk();
});
byId("resetCalculatorButton").addEventListener("click", resetCalculator);
byId("useEaParametersButton").addEventListener("click", () => useEaParameters(true));
form.addEventListener("input", (event) => {
  if (!event.target.closest(".trading-mode-admin-panel") && !event.target.closest(".user-entry-mode-section")) formDirty = true;
});
form.addEventListener("submit", (event) => { event.preventDefault(); saveConfig(false); });
scheduleForm.addEventListener("input", () => { scheduleDirty = true; });
scheduleForm.addEventListener("submit", (event) => { event.preventDefault(); saveSchedule(); });
byId("refreshProtectionButton").addEventListener("click", () => saveConfig(true));
byId("saveDailyProfitAutoStopButton").addEventListener("click", saveDailyProfitAutoStop);
byId("dailyProfitAutoStopEnabled").addEventListener("change", () => { dailyProfitAutoStopDirty = true; });
byId("dailyProfitTarget").addEventListener("input", () => { dailyProfitAutoStopDirty = true; });
byId("startButton").addEventListener("click", () => setControl("start"));
byId("stopButton").addEventListener("click", () => setControl("stop"));
stopMode.addEventListener("change", () => {
  stopModeDirty = stopMode.value !== serverStopMode;
  updateStopModeHint();
});

initializeSession();
calculateEaRisk();
setInterval(refreshDashboardFallback, DASHBOARD_REFRESH_MS);
setInterval(refreshAdminUserConnections, ADMIN_USERS_REFRESH_MS);
setInterval(updateScheduleCountdown, 500);
setInterval(updateAccountExpirationCountdown, 1000);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  startDashboardStream();
  refreshDashboard();
});
window.addEventListener("focus", () => {
  startDashboardStream();
  refreshDashboard();
});
