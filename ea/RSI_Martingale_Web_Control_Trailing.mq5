//+------------------------------------------------------------------+
//| RSI_Martingale_Web_Control_Trailing.mq5                         |
//| RSI martingale with shared BE trailing and FastAPI WebUI        |
//+------------------------------------------------------------------+
#property version   "2.543"
#property strict

// Defaults are used until the first successful API poll.
input group "Web Control"
input string ApiBaseUrl       = "https://ea.agoy507.my.id/";
input string EaId             = "rsi-martingale-xauusd-m1";
input string ApiToken          = "";
input double ApiPollSeconds   = 0.5;
input int    ApiTimeoutMs     = 1500;
input double ApiStaleTimeoutSeconds = 10.0;
input ulong  MagicNumber      = 20260827;

input group "Default RSI Settings"
input int RSI_Periode         = 5;
input int RSI_Nilai_Beli      = 49;
input int RSI_Nilai_Jual      = 50;

input group "Default Trading Settings"
input double Lot_Awal         = 0.01;
input int SL_Awal_Pips        = 300;
input int TP_Layer_Pertama_Pips = 20;

input group "Default Group Trailing Settings"
input int Trailing_Mulai_Pips = 10;
input int Trailing_Jarak_Pips = 5;
input int Trailing_Geser_Pips = 3;

input group "Default Martingale Settings"
input bool Aktifkan_Martingale       = true;
input int Jarak_Pips_Martingale      = 30;
input double Pengali_Lot             = 1.5;
input int Maksimal_Martingale        = 10;
input double Lot_Maksimal            = 1.0;

const double NILAI_1_PIP_XAUUSD = 0.10;
// Retcode 10026 means the trade server has disabled automated trading.
// New entry retries back off from 1 minute up to 60 minutes so the EA does
// not hammer the broker server while the restriction is active.
const ulong RETCODE_10026_INITIAL_BACKOFF_MS = 60000;
const ulong RETCODE_10026_MAX_BACKOFF_MS = 3600000;

int handle_RSI = INVALID_HANDLE;

// Runtime configuration controlled by the WebUI.
int cfgRsiPeriod;
int cfgRsiBuyLevel;
int cfgRsiSellLevel;
double cfgInitialLot;
int cfgInitialSlPips;
int cfgFirstLayerTpPips;
int cfgTrailingStartPips;
int cfgTrailingDistancePips;
int cfgTrailingStepPips;
bool cfgMartingaleEnabled;
int cfgMartingaleDistancePips;
double cfgLotMultiplier;
long cfgMaxMartingale;
double cfgMaxLot;
bool cfgAlgoEnabled = false;
string cfgStopMode = "delete_pending";

enum ENUM_EA_ERROR_SOURCE
{
   EA_ERROR_NONE = 0,
   EA_ERROR_API,
   EA_ERROR_CONFIG,
   EA_ERROR_OPEN,
   EA_ERROR_PROTECTION,
   EA_ERROR_STOP
};

// Lot cycle lock: initial lot + multiplier are frozen for the lifetime of
// an active BUY/SELL cycle. WebUI changes become active on the next cycle.
double cycleInitialLot = 0.0;
double cycleLotMultiplier = 0.0;
bool cycleLotLocked = false;

long lastConfigRevision = -1;
long lastCommandRevision = -1;
long lastRefreshProtectionRevision = -1;
double currentRsi = 0.0;
double liveRsi = 0.0;
bool liveRsiAvailable = false;
double cachedDailyProfit = 0.0;
double cachedAccountDailyProfit = 0.0;
string cachedDailyProfitDate = "";
bool cachedDailyProfitAvailable = false;
ulong lastDailyProfitRefreshMs = 0;
const ulong DAILY_PROFIT_REFRESH_MS = 5000;
datetime lastProtectionUpdate = 0;
datetime lastCloseAllRetry = 0;
double groupStopBuy = 0.0;
double groupStopSell = 0.0;
bool trailingActiveBuy = false;
bool trailingActiveSell = false;
datetime lastTrailingProtectionAttempt = 0;
string lastErrorMessage = "";
ENUM_EA_ERROR_SOURCE lastErrorSource = EA_ERROR_NONE;
bool serverStateSynced = false;
// Jika satu layer terkena SL broker, semua layer pada arah yang sama harus ditutup.
bool closeAllAfterProtectedExitBuy = false;
bool closeAllAfterProtectedExitSell = false;
const int CLOSE_ALL_RETRY_SECONDS = 1;
ulong lastSuccessfulPollMs = 0;
string clientInstanceId = "";
ulong entryRetryBlockedUntilMs = 0;
int serverDisableEntryStrikes = 0;
datetime lastEntryAttemptTime = 0;
const int ENTRY_SAFE_DELAY_SECONDS = 3;

//+------------------------------------------------------------------+
int OnInit()
{
   ENUM_ACCOUNT_MARGIN_MODE margin_mode = (ENUM_ACCOUNT_MARGIN_MODE)AccountInfoInteger(ACCOUNT_MARGIN_MODE);
   if(margin_mode != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
   {
      Print("ERROR: EA membutuhkan akun MT5 mode hedging agar setiap tahap martingale tetap terpisah.");
      return INIT_FAILED;
   }

   LoadDefaults();
   clientInstanceId = StringFormat("%I64d-%I64d-%I64u",
                                   AccountInfoInteger(ACCOUNT_LOGIN),
                                   ChartID(),
                                   GetMicrosecondCount());
   if(!CreateRsiHandle())
      return INIT_FAILED;

   // Restore a previously locked cycle after EA/MT5 restart.
   // If no saved state exists while positions are already running, recovery
   // will be completed after the first successful WebUI configuration poll.
   LoadCycleLotState();

   if(StringLen(ApiToken) == 0)
      Print("WARNING: ApiToken kosong. EA Trailing V2.5.4.2 tidak dapat terhubung sebelum token user dimasukkan.");

   int api_poll_ms = (int)MathMax(100.0, MathRound(ApiPollSeconds * 1000.0));
   if(!EventSetMillisecondTimer(api_poll_ms))
   {
      Print("ERROR: Timer API gagal dibuat. Error=", GetLastError());
      return INIT_FAILED;
   }
   Print("EA RSI Martingale Trailing Web Control siap. Magic=", MagicNumber,
          " | API=", ApiBaseUrl, " | Poll=", api_poll_ms,
         " ms | Client=", clientInstanceId, " | 1 pip XAUUSD=0.10");
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
   if(handle_RSI != INVALID_HANDLE)
      IndicatorRelease(handle_RSI);
   Print("EA dihentikan. Reason=", reason);
}

//+------------------------------------------------------------------+
void OnTimer()
{
   UpdateLiveRsi();
   string response = "";
   if(PostStatus(response) && ApplyServerState(response))
   {
      serverStateSynced = true;
      lastSuccessfulPollMs = GetTickCount64();
      return;
   }

   // Compatibility fallback for V2.5.3 and older servers whose status endpoint
   // does not return the current control/config payload.
   string account_login = IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN));
   string path = "/api/ea/poll?ea_id=" + EaId +
                 "&account_login=" + account_login +
                 "&client_id=" + clientInstanceId;
   if(!HttpRequest("GET", path, "", response))
      return;

   if(!ApplyServerState(response))
      return;

   serverStateSynced = true;
   lastSuccessfulPollMs = GetTickCount64();
}

//+------------------------------------------------------------------+
bool ServerControlFresh()
{
   if(!serverStateSynced)
      return false;
   ulong timeout_ms = (ulong)MathMax(1000.0, MathRound(ApiStaleTimeoutSeconds * 1000.0));
   return GetTickCount64() - lastSuccessfulPollMs <= timeout_ms;
}

//+------------------------------------------------------------------+
void RecordError(const string message, const ENUM_EA_ERROR_SOURCE source)
{
   lastErrorMessage = message;
   lastErrorSource = source;
   Print(lastErrorMessage);
}

//+------------------------------------------------------------------+
void ClearRecordedError(const ENUM_EA_ERROR_SOURCE source)
{
   if(lastErrorSource != source)
      return;
   lastErrorMessage = "";
   lastErrorSource = EA_ERROR_NONE;
}

//+------------------------------------------------------------------+
bool EntryAttemptAllowed()
{
   if(entryRetryBlockedUntilMs == 0)
      return true;

   ulong now_ms = GetTickCount64();
   if(now_ms < entryRetryBlockedUntilMs)
      return false;

   entryRetryBlockedUntilMs = 0;
   Print("Proteksi retcode 10026: masa jeda selesai, satu percobaan entry diizinkan.");
   return true;
}

//+------------------------------------------------------------------+
void PauseEntryAfterServerDisable()
{
   serverDisableEntryStrikes++;
   int exponent = (int)MathMin(serverDisableEntryStrikes - 1, 6);
   double candidate_ms = (double)RETCODE_10026_INITIAL_BACKOFF_MS * MathPow(2.0, exponent);
   ulong pause_ms = (ulong)MathMin(candidate_ms, (double)RETCODE_10026_MAX_BACKOFF_MS);
   entryRetryBlockedUntilMs = GetTickCount64() + pause_ms;
   int pause_minutes = (int)MathCeil((double)pause_ms / 60000.0);
   RecordError("Retcode 10026: autotrading dinonaktifkan server broker. Entry dijeda " +
               IntegerToString(pause_minutes) + " menit.",
               EA_ERROR_OPEN);
}

//+------------------------------------------------------------------+
void ResetServerDisableEntryBackoff()
{
   entryRetryBlockedUntilMs = 0;
   serverDisableEntryStrikes = 0;
}

//+------------------------------------------------------------------+
// TP layer 1 atau SL layer 2 mengakhiri seluruh siklus pada arah itu.
// Close pertama mengirim request bagi semua layer tersisa sekaligus; hanya
// order yang gagal akan dicoba lagi dengan jeda agar tidak spam broker.
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD || trans.deal == 0 || !HistoryDealSelect(trans.deal))
      return;
   if((ulong)HistoryDealGetInteger(trans.deal, DEAL_MAGIC) != MagicNumber)
      return;
   ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
   ENUM_DEAL_REASON reason = (ENUM_DEAL_REASON)HistoryDealGetInteger(trans.deal, DEAL_REASON);
   if((entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY) ||
      (reason != DEAL_REASON_SL && reason != DEAL_REASON_TP))
      return;
   ENUM_DEAL_TYPE deal_type = (ENUM_DEAL_TYPE)HistoryDealGetInteger(trans.deal, DEAL_TYPE);
   if(deal_type == DEAL_TYPE_SELL)
      closeAllAfterProtectedExitBuy = true;
   else if(deal_type == DEAL_TYPE_BUY)
      closeAllAfterProtectedExitSell = true;
}

//+------------------------------------------------------------------+
void ProcessCloseAllAfterProtectedExit()
{
   if(!closeAllAfterProtectedExitBuy && !closeAllAfterProtectedExitSell)
      return;
   if(TimeCurrent() - lastCloseAllRetry < CLOSE_ALL_RETRY_SECONDS)
      return;
   lastCloseAllRetry = TimeCurrent();
   if(closeAllAfterProtectedExitBuy)
   {
      CloseOwnedPositions(ORDER_TYPE_BUY);
      closeAllAfterProtectedExitBuy = HasPosition(ORDER_TYPE_BUY);
   }
   if(closeAllAfterProtectedExitSell)
   {
      CloseOwnedPositions(ORDER_TYPE_SELL);
      closeAllAfterProtectedExitSell = HasPosition(ORDER_TYPE_SELL);
   }
}

//+------------------------------------------------------------------+
void OnTick()
{
   ProcessCloseAllAfterProtectedExit();
   if(closeAllAfterProtectedExitBuy || closeAllAfterProtectedExitSell)
      return;
   UpdateLiveRsi();
   bool has_buy = HasPosition(ORDER_TYPE_BUY);
   bool has_sell = HasPosition(ORDER_TYPE_SELL);

   if(!has_buy && !has_sell && cycleLotLocked)
   {
      ClearCycleLotState();
      groupStopBuy = 0.0;
      groupStopSell = 0.0;
      trailingActiveBuy = false;
      trailingActiveSell = false;
   }

   if(!cfgAlgoEnabled && cfgStopMode == "close_all")
   {
      if((has_buy || has_sell || HasOwnedPendingOrders()) &&
         TimeCurrent() - lastCloseAllRetry >= 1)
      {
         ApplyStopMode(cfgStopMode);
      }
      return;
   }

   // Trailing and the group stop remain active even if WebUI has stopped new
   // cycles or its connection becomes stale. This is intentionally separate
   // from the entry permission below.
   if(has_buy)
      ManageGroupTrailing(ORDER_TYPE_BUY);
   if(has_sell)
      ManageGroupTrailing(ORDER_TYPE_SELL);

   // TP layer 1 selalu mengikuti BEP rata-rata selama trailing belum aktif.
   // Request hanya dikirim jika TP benar-benar berubah.
   if(HasPosition(ORDER_TYPE_BUY))
      UpdateFirstLayerTP(ORDER_TYPE_BUY);
   if(HasPosition(ORDER_TYPE_SELL))
      UpdateFirstLayerTP(ORDER_TYPE_SELL);

   has_buy = HasPosition(ORDER_TYPE_BUY);
   has_sell = HasPosition(ORDER_TYPE_SELL);
   if(!has_buy && !has_sell)
   {
      groupStopBuy = 0.0;
      groupStopSell = 0.0;
      trailingActiveBuy = false;
      trailingActiveSell = false;
      if(cycleLotLocked)
         ClearCycleLotState();
   }

   if(!has_buy && !has_sell)
   {
      if(!cfgAlgoEnabled || !ServerControlFresh())
         return;

      double values[];
      ArraySetAsSeries(values, true);
      if(CopyBuffer(handle_RSI, 0, 1, 2, values) < 2)
         return;
      currentRsi = values[0];

      if(currentRsi <= cfgRsiBuyLevel)
      {
         BeginCycleLotState();
         if(!OpenPosition(ORDER_TYPE_BUY, cycleInitialLot))
            ClearCycleLotState();
         return;
      }
      if(currentRsi >= cfgRsiSellLevel)
      {
         BeginCycleLotState();
         if(!OpenPosition(ORDER_TYPE_SELL, cycleInitialLot))
            ClearCycleLotState();
         return;
      }

      return;
   }

   if(!cfgMartingaleEnabled)
      return;

   if((has_buy || has_sell) && !cycleLotLocked)
      return;

   int buy_stage = CountMartingaleStages(ORDER_TYPE_BUY);
   int sell_stage = CountMartingaleStages(ORDER_TYPE_SELL);
   if(has_buy && buy_stage < cfgMaxMartingale)
      CheckBuyMartingale(buy_stage);
   if(has_sell && sell_stage < cfgMaxMartingale)
      CheckSellMartingale(sell_stage);
}

//+------------------------------------------------------------------+
bool UpdateLiveRsi()
{
   if(handle_RSI == INVALID_HANDLE)
      return false;
   double values[];
   ArraySetAsSeries(values, true);
   if(CopyBuffer(handle_RSI, 0, 0, 1, values) < 1)
      return false;
   if(values[0] == EMPTY_VALUE || !MathIsValidNumber(values[0]))
      return false;
   liveRsi = values[0];
   liveRsiAvailable = true;
   return true;
}

//+------------------------------------------------------------------+
void LoadDefaults()
{
   cfgRsiPeriod = RSI_Periode;
   cfgRsiBuyLevel = RSI_Nilai_Beli;
   cfgRsiSellLevel = RSI_Nilai_Jual;
   cfgInitialLot = Lot_Awal;
   cfgInitialSlPips = SL_Awal_Pips;
   cfgFirstLayerTpPips = TP_Layer_Pertama_Pips;
   cfgTrailingStartPips = Trailing_Mulai_Pips;
   cfgTrailingDistancePips = Trailing_Jarak_Pips;
   cfgTrailingStepPips = Trailing_Geser_Pips;
   cfgMartingaleEnabled = Aktifkan_Martingale;
   cfgMartingaleDistancePips = Jarak_Pips_Martingale;
   cfgLotMultiplier = Pengali_Lot;
   cfgMaxMartingale = Maksimal_Martingale;
   cfgMaxLot = Lot_Maksimal;
}

//+------------------------------------------------------------------+
bool CreateRsiHandle()
{
   if(handle_RSI != INVALID_HANDLE)
      IndicatorRelease(handle_RSI);

   handle_RSI = iRSI(_Symbol, PERIOD_M1, cfgRsiPeriod, PRICE_CLOSE);
   if(handle_RSI == INVALID_HANDLE)
   {
      RecordError("Gagal membuat handle RSI. Error=" + IntegerToString(GetLastError()),
                  EA_ERROR_CONFIG);
      return false;
   }
   ClearRecordedError(EA_ERROR_CONFIG);
   return true;
}

//+------------------------------------------------------------------+
bool ApplyServerState(const string json)
{
   long config_revision;
   long command_revision;
   long refresh_revision;
   bool server_algo_enabled;
   string stop_mode;
   string strategy_type;

   if(!JsonGetLong(json, "config_revision", config_revision) ||
      !JsonGetLong(json, "command_revision", command_revision) ||
      !JsonGetLong(json, "refresh_protection_revision", refresh_revision) ||
      !JsonGetBool(json, "algo_enabled", server_algo_enabled) ||
      !JsonGetString(json, "stop_mode", stop_mode))
   {
      RecordError("Respons API tidak lengkap atau format JSON tidak dikenali.",
                  EA_ERROR_API);
      Print("Payload API=", json);
      return false;
   }

   if(!JsonGetString(json, "strategy_type", strategy_type) || strategy_type != "trailing")
   {
      RecordError("Jenis EA di server bukan trailing. Pilih Trailing di Kelola User sebelum menjalankan file ini.",
                  EA_ERROR_CONFIG);
      return false;
   }

   if(config_revision != lastConfigRevision)
   {
      if(!ApplyConfigJson(json))
         return false;
      lastConfigRevision = config_revision;
      Print("Konfigurasi WebUI diterapkan. Revision=", config_revision);
   }

   if(command_revision != lastCommandRevision)
   {
      bool was_enabled = cfgAlgoEnabled;
      cfgAlgoEnabled = server_algo_enabled;
      cfgStopMode = stop_mode;
      if(!cfgAlgoEnabled)
         ApplyStopMode(stop_mode);
      else if(cfgAlgoEnabled && !was_enabled)
         Print("Perintah START diterapkan.");
      lastCommandRevision = command_revision;
   }
   else
   {
      cfgAlgoEnabled = server_algo_enabled;
      cfgStopMode = stop_mode;
   }

   if(refresh_revision != lastRefreshProtectionRevision)
   {
      if(refresh_revision > 0)
      {
         UpdateTrailingProtection();
         Print("Refresh SL/TP dari WebUI diterapkan. Revision=", refresh_revision);
      }
      lastRefreshProtectionRevision = refresh_revision;
   }

   return true;
}

//+------------------------------------------------------------------+
bool ApplyConfigJson(const string json)
{
   long int_value;
   double double_value;
   bool bool_value;
   int previous_period = cfgRsiPeriod;

   if(!JsonGetLong(json, "rsi_period", int_value)) return ConfigError("rsi_period");
   cfgRsiPeriod = (int)int_value;
   if(!JsonGetLong(json, "rsi_buy_level", int_value)) return ConfigError("rsi_buy_level");
   cfgRsiBuyLevel = (int)int_value;
   if(!JsonGetLong(json, "rsi_sell_level", int_value)) return ConfigError("rsi_sell_level");
   cfgRsiSellLevel = (int)int_value;
   if(!JsonGetDouble(json, "initial_lot", double_value)) return ConfigError("initial_lot");
   cfgInitialLot = double_value;
   if(!JsonGetLong(json, "initial_sl_pips", int_value)) return ConfigError("initial_sl_pips");
   cfgInitialSlPips = (int)int_value;
   if(!JsonGetLong(json, "first_layer_tp_pips", int_value)) return ConfigError("first_layer_tp_pips");
   cfgFirstLayerTpPips = (int)int_value;
   if(!JsonGetLong(json, "trailing_start_pips", int_value)) return ConfigError("trailing_start_pips");
   cfgTrailingStartPips = (int)int_value;
   if(!JsonGetLong(json, "trailing_distance_pips", int_value)) return ConfigError("trailing_distance_pips");
   cfgTrailingDistancePips = (int)int_value;
   if(!JsonGetLong(json, "trailing_step_pips", int_value)) return ConfigError("trailing_step_pips");
   cfgTrailingStepPips = (int)int_value;
   if(!JsonGetBool(json, "martingale_enabled", bool_value)) return ConfigError("martingale_enabled");
   cfgMartingaleEnabled = bool_value;
   if(!JsonGetLong(json, "martingale_distance_pips", int_value)) return ConfigError("martingale_distance_pips");
   cfgMartingaleDistancePips = (int)int_value;
   if(!JsonGetDouble(json, "lot_multiplier", double_value)) return ConfigError("lot_multiplier");
   cfgLotMultiplier = double_value;
   if(!JsonGetLong(json, "max_martingale", int_value)) return ConfigError("max_martingale");
   cfgMaxMartingale = int_value;
   if(!JsonGetDouble(json, "max_lot", double_value)) return ConfigError("max_lot");
   cfgMaxLot = double_value;

   if(cfgRsiPeriod < 2 || cfgRsiBuyLevel >= cfgRsiSellLevel ||
      cfgInitialLot <= 0 || cfgMaxLot < cfgInitialLot ||
      cfgInitialSlPips <= 0 || cfgFirstLayerTpPips <= 0 || cfgTrailingStartPips <= 0 ||
      cfgTrailingDistancePips <= 0 || cfgTrailingStepPips <= 0 ||
      cfgTrailingDistancePips > cfgTrailingStartPips ||
      cfgMartingaleDistancePips <= 0 || cfgLotMultiplier < 1.0 ||
      cfgMaxMartingale < 0)
   {
      RecordError("Konfigurasi API ditolak oleh validasi lokal EA.",
                  EA_ERROR_CONFIG);
      return false;
   }

   if(previous_period != cfgRsiPeriod && !CreateRsiHandle())
      return false;

   // Recovery path for upgrading/re-attaching this EA while a cycle already
   // exists but no persistent lock has been saved yet. The initial lot is
   // recovered from the oldest owned position; the multiplier is frozen from
   // the current server configuration from this point onward.
   if((HasPosition(ORDER_TYPE_BUY) || HasPosition(ORDER_TYPE_SELL)) && !cycleLotLocked)
      RecoverCycleLotState();

   ClearRecordedError(EA_ERROR_CONFIG);
   return true;
}

//+------------------------------------------------------------------+
bool ConfigError(const string field_name)
{
   RecordError("Field konfigurasi hilang/tidak valid: " + field_name,
               EA_ERROR_CONFIG);
   return false;
}

//+------------------------------------------------------------------+
bool ApplyStopMode(const string stop_mode)
{
   Print("Perintah STOP diterapkan. Mode=", stop_mode);
   bool success = true;
   if(stop_mode == "delete_pending" || stop_mode == "close_all")
      success = DeleteOwnedPendingOrders() && success;
   if(stop_mode == "close_all")
   {
      success = CloseAllOwnedPositions() && success;
      lastCloseAllRetry = TimeCurrent();
   }
   if(success)
      ClearRecordedError(EA_ERROR_STOP);
   return success;
}

//+------------------------------------------------------------------+
int CountMartingaleStages(ENUM_ORDER_TYPE type)
{
   // Initial position is stage 0; each additional position adds one stage.
   return (int)MathMax(0, CountPositions(type) - 1);
}

//+------------------------------------------------------------------+
int CountPositions(ENUM_ORDER_TYPE type)
{
   int count = 0;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(IsOwnedPosition(ticket) && PositionGetInteger(POSITION_TYPE) == type)
         count++;
   }
   return count;
}

//+------------------------------------------------------------------+
bool HasPosition(ENUM_ORDER_TYPE type)
{
   return CountPositions(type) > 0;
}

//+------------------------------------------------------------------+
bool IsOwnedPosition(const ulong ticket)
{
   if(ticket == 0 || !PositionSelectByTicket(ticket))
      return false;
   return PositionGetString(POSITION_SYMBOL) == _Symbol &&
          (ulong)PositionGetInteger(POSITION_MAGIC) == MagicNumber;
}

//+------------------------------------------------------------------+
double CalculateMartingaleLot(const int stage)
{
   double initial_lot = cycleLotLocked ? cycleInitialLot : cfgInitialLot;
   double multiplier  = cycleLotLocked ? cycleLotMultiplier : cfgLotMultiplier;
   double lot = initial_lot * MathPow(multiplier, stage);
   lot = MathMin(lot, cfgMaxLot);
   // Match RSI_Martingale_Web_Control.mq5: calculate each stage from the
   // locked initial lot, then round UP to the broker volume step.
   return MathMin(NormalizeVolumeUp(lot), NormalizeVolume(cfgMaxLot));
}

//+------------------------------------------------------------------+
string CycleGlobalPrefix()
{
   return StringFormat("RSIWC_%s_%I64u_", _Symbol, MagicNumber);
}

//+------------------------------------------------------------------+
void SaveCycleLotState()
{
   string prefix = CycleGlobalPrefix();
   GlobalVariableSet(prefix + "IL", cycleInitialLot);
   GlobalVariableSet(prefix + "LM", cycleLotMultiplier);
}

//+------------------------------------------------------------------+
bool LoadCycleLotState()
{
   string prefix = CycleGlobalPrefix();
   string key_initial = prefix + "IL";
   string key_multiplier = prefix + "LM";

   if(!GlobalVariableCheck(key_initial) || !GlobalVariableCheck(key_multiplier))
      return false;

   // A stored lock only belongs to an actually running owned cycle.
   if(!HasPosition(ORDER_TYPE_BUY) && !HasPosition(ORDER_TYPE_SELL))
   {
      GlobalVariableDel(key_initial);
      GlobalVariableDel(key_multiplier);
      return false;
   }

   double saved_initial = GlobalVariableGet(key_initial);
   double saved_multiplier = GlobalVariableGet(key_multiplier);
   if(saved_initial <= 0.0 || saved_multiplier < 1.0)
      return false;

   cycleInitialLot = saved_initial;
   cycleLotMultiplier = saved_multiplier;
   cycleLotLocked = true;
   Print("Cycle lot dipulihkan: Lot Awal=", cycleInitialLot,
         " | Pengali=", cycleLotMultiplier);
   return true;
}

//+------------------------------------------------------------------+
void BeginCycleLotState()
{
   cycleInitialLot = cfgInitialLot;
   cycleLotMultiplier = cfgLotMultiplier;
   cycleLotLocked = true;
   SaveCycleLotState();
   Print("Cycle lot dikunci: Lot Awal=", cycleInitialLot,
         " | Pengali=", cycleLotMultiplier);
}

//+------------------------------------------------------------------+
double GetOldestOwnedPositionVolume()
{
   double oldest_volume = 0.0;
   long oldest_time_msc = -1;

   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(!IsOwnedPosition(ticket))
         continue;

      long time_msc = PositionGetInteger(POSITION_TIME_MSC);
      if(oldest_time_msc < 0 || time_msc < oldest_time_msc)
      {
         oldest_time_msc = time_msc;
         oldest_volume = PositionGetDouble(POSITION_VOLUME);
      }
   }
   return oldest_volume;
}

//+------------------------------------------------------------------+
void RecoverCycleLotState()
{
   if(cycleLotLocked)
      return;

   double oldest_lot = GetOldestOwnedPositionVolume();
   if(oldest_lot <= 0.0)
      return;

   cycleInitialLot = oldest_lot;
   cycleLotMultiplier = cfgLotMultiplier;
   cycleLotLocked = true;
   SaveCycleLotState();
   Print("Cycle lot direcovery dari posisi aktif: Lot Awal=", cycleInitialLot,
         " | Pengali dikunci=", cycleLotMultiplier);
}

//+------------------------------------------------------------------+
void ClearCycleLotState()
{
   string prefix = CycleGlobalPrefix();
   GlobalVariableDel(prefix + "IL");
   GlobalVariableDel(prefix + "LM");
   cycleInitialLot = 0.0;
   cycleLotMultiplier = 0.0;
   cycleLotLocked = false;
   Print("Cycle selesai: lock Lot Awal + Pengali dilepas.");
}

//+------------------------------------------------------------------+
double NormalizeVolume(double volume)
{
   double minimum = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maximum = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0)
      step = 0.01;
   volume = MathMax(minimum, MathMin(maximum, volume));
   volume = MathFloor(volume / step + 1e-8) * step;
   return NormalizeDouble(volume, VolumeDigits(step));
}

//+------------------------------------------------------------------+
double NormalizeVolumeUp(double volume)
{
   double minimum = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maximum = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0)
      step = 0.01;
   volume = MathMax(minimum, MathMin(maximum, volume));
   volume = MathCeil(volume / step - 1e-8) * step;
   volume = MathMax(minimum, MathMin(maximum, volume));
   return NormalizeDouble(volume, VolumeDigits(step));
}

//+------------------------------------------------------------------+
int VolumeDigits(const double step)
{
   int digits = 0;
   double scaled = step;
   while(digits < 8 && MathAbs(scaled - MathRound(scaled)) > 1e-8)
   {
      scaled *= 10.0;
      digits++;
   }
   return digits;
}

//+------------------------------------------------------------------+
void CheckBuyMartingale(const int current_stage)
{
   double last_price = GetLatestOpenPrice(ORDER_TYPE_BUY);
   if(last_price <= 0.0)
      return;
   double distance = cfgMartingaleDistancePips * NILAI_1_PIP_XAUUSD;
   double distance_now = last_price - SymbolInfoDouble(_Symbol, SYMBOL_BID);
   if(distance_now < distance)
      return;

   double lot = CalculateMartingaleLot(CountPositions(ORDER_TYPE_BUY));
   Print("Buka Martingale | BUY | Order=", current_stage + 1,
         " | Jarak=", DoubleToString(distance_now / NILAI_1_PIP_XAUUSD, 1),
         " pips | Lot=", DoubleToString(lot, 2));
   OpenPosition(ORDER_TYPE_BUY, lot);
}

//+------------------------------------------------------------------+
void CheckSellMartingale(const int current_stage)
{
   double last_price = GetLatestOpenPrice(ORDER_TYPE_SELL);
   if(last_price <= 0.0)
      return;
   double distance = cfgMartingaleDistancePips * NILAI_1_PIP_XAUUSD;
   double distance_now = SymbolInfoDouble(_Symbol, SYMBOL_ASK) - last_price;
   if(distance_now < distance)
      return;

   double lot = CalculateMartingaleLot(CountPositions(ORDER_TYPE_SELL));
   Print("Buka Martingale | SELL | Order=", current_stage + 1,
         " | Jarak=", DoubleToString(distance_now / NILAI_1_PIP_XAUUSD, 1),
         " pips | Lot=", DoubleToString(lot, 2));
   OpenPosition(ORDER_TYPE_SELL, lot);
}

//+------------------------------------------------------------------+
double GetLatestOpenPrice(ENUM_ORDER_TYPE type)
{
   double latest_price = 0.0;
   long latest_time_msc = -1;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(IsOwnedPosition(ticket) && PositionGetInteger(POSITION_TYPE) == type)
      {
         long time_msc = PositionGetInteger(POSITION_TIME_MSC);
         if(time_msc > latest_time_msc)
         {
            latest_time_msc = time_msc;
            latest_price = PositionGetDouble(POSITION_PRICE_OPEN);
         }
      }
   }
   return latest_price;
}

//+------------------------------------------------------------------+
double GetLatestOpenVolume(ENUM_ORDER_TYPE type)
{
   double latest_volume = 0.0;
   long latest_time_msc = -1;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(IsOwnedPosition(ticket) && PositionGetInteger(POSITION_TYPE) == type)
      {
         long time_msc = PositionGetInteger(POSITION_TIME_MSC);
         if(time_msc > latest_time_msc)
         {
            latest_time_msc = time_msc;
            latest_volume = PositionGetDouble(POSITION_VOLUME);
         }
      }
   }
   return latest_volume;
}

//+------------------------------------------------------------------+
void GetWeightedAveragePrices(double &buy_average, double &buy_lots,
                              double &sell_average, double &sell_lots)
{
   buy_average = 0.0;
   buy_lots = 0.0;
   sell_average = 0.0;
   sell_lots = 0.0;

   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(!IsOwnedPosition(ticket))
         continue;

      ENUM_ORDER_TYPE type = (ENUM_ORDER_TYPE)PositionGetInteger(POSITION_TYPE);
      double price = PositionGetDouble(POSITION_PRICE_OPEN);
      double lots = PositionGetDouble(POSITION_VOLUME);
      if(type == ORDER_TYPE_BUY)
      {
         buy_average = (buy_average * buy_lots + price * lots) / (buy_lots + lots);
         buy_lots += lots;
      }
      else if(type == ORDER_TYPE_SELL)
      {
         sell_average = (sell_average * sell_lots + price * lots) / (sell_lots + lots);
         sell_lots += lots;
      }
   }
}

//+------------------------------------------------------------------+
double GetTrackedGroupStop(const ENUM_ORDER_TYPE type)
{
   return type == ORDER_TYPE_BUY ? groupStopBuy : groupStopSell;
}

//+------------------------------------------------------------------+
void SetTrackedGroupStop(const ENUM_ORDER_TYPE type, const double stop)
{
   if(type == ORDER_TYPE_BUY)
      groupStopBuy = stop;
   else
      groupStopSell = stop;
}

//+------------------------------------------------------------------+
bool IsTrailingActive(const ENUM_ORDER_TYPE type)
{
   return type == ORDER_TYPE_BUY ? trailingActiveBuy : trailingActiveSell;
}

//+------------------------------------------------------------------+
void SetTrailingActive(const ENUM_ORDER_TYPE type, const bool active)
{
   if(type == ORDER_TYPE_BUY)
      trailingActiveBuy = active;
   else
      trailingActiveSell = active;
}

//+------------------------------------------------------------------+
double ExistingGroupStop(const ENUM_ORDER_TYPE type)
{
   double result = 0.0;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(!IsOwnedPosition(ticket) || PositionGetInteger(POSITION_TYPE) != type)
         continue;
      double stop = PositionGetDouble(POSITION_SL);
      if(stop <= 0.0)
         continue;
      if(result <= 0.0 || (type == ORDER_TYPE_BUY && stop > result) ||
         (type == ORDER_TYPE_SELL && stop < result))
         result = stop;
   }
   return result;
}

//+------------------------------------------------------------------+
// Update TP layer pertama berdasarkan harga rata-rata seluruh layer.
// Hanya layer pertama yang diberi TP; layer 2+ tetap TP=0.
bool UpdateFirstLayerTP(const ENUM_ORDER_TYPE type)
{
   if(!HasPosition(type))
      return true;

   // Setelah trailing aktif, TP tidak lagi dipindahkan.
   if(IsTrailingActive(type))
      return true;

   double buy_average, buy_lots, sell_average, sell_lots;
   GetWeightedAveragePrices(buy_average, buy_lots, sell_average, sell_lots);
   double average = type == ORDER_TYPE_BUY ? buy_average : sell_average;
   if(average <= 0.0)
      return false;

   ulong first_ticket = 0;
   long first_time_msc = 0;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(!IsOwnedPosition(ticket) || PositionGetInteger(POSITION_TYPE) != type)
         continue;

      long time_msc = PositionGetInteger(POSITION_TIME_MSC);
      if(first_ticket == 0 || time_msc < first_time_msc)
      {
         first_ticket = ticket;
         first_time_msc = time_msc;
      }
   }

   if(first_ticket == 0 || !PositionSelectByTicket(first_ticket))
      return false;

   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double distance = cfgFirstLayerTpPips * NILAI_1_PIP_XAUUSD;
   double target_tp = NormalizeDouble(type == ORDER_TYPE_BUY
                                      ? average + distance
                                      : average - distance, digits);
   double current_tp = PositionGetDouble(POSITION_TP);

   // Tidak perlu mengirim request jika TP belum berubah.
   if(MathAbs(current_tp - target_tp) < SymbolInfoDouble(_Symbol, SYMBOL_POINT) / 2.0)
      return true;

   MqlTradeRequest request = {};
   MqlTradeResult result = {};
   request.action = TRADE_ACTION_SLTP;
   request.symbol = _Symbol;
   request.position = first_ticket;
   request.magic = MagicNumber;
   request.sl = PositionGetDouble(POSITION_SL);
   request.tp = target_tp;

   ResetLastError();
   if(!OrderSend(request, result) || !TradeRetcodeAccepted(result.retcode))
   {
      RecordError("Gagal memindahkan TP layer 1 ticket=" + IntegerToString((long)first_ticket) +
                  " retcode=" + IntegerToString((long)result.retcode),
                  EA_ERROR_PROTECTION);
      return false;
   }

   Print("TP Layer 1 dipindahkan | ", type == ORDER_TYPE_BUY ? "BUY" : "SELL",
         " | Avg=", DoubleToString(average, digits),
         " | TP baru=", DoubleToString(target_tp, digits));
   return true;
}

//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
bool ApplySecondLayerStop(const ENUM_ORDER_TYPE type, const double stop)
{
   // Layer 1 keeps TP. Layer 2 permanently owns SL even when later
   // martingale layers open; only the SL price itself is recalculated.
   ulong first_ticket = 0;
   ulong second_ticket = 0;
   long first_time_msc = 0;
   long second_time_msc = 0;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(!IsOwnedPosition(ticket) || PositionGetInteger(POSITION_TYPE) != type)
         continue;
      long time_msc = PositionGetInteger(POSITION_TIME_MSC);
      if(first_ticket == 0 || time_msc < first_time_msc)
      {
         second_ticket = first_ticket;
         second_time_msc = first_time_msc;
         first_ticket = ticket;
         first_time_msc = time_msc;
      }
      else if(second_ticket == 0 || time_msc < second_time_msc)
      {
         second_ticket = ticket;
         second_time_msc = time_msc;
      }
   }
   if(second_ticket == 0)
      return true;

   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   bool success = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(!IsOwnedPosition(ticket) || PositionGetInteger(POSITION_TYPE) != type || !PositionSelectByTicket(ticket))
         continue;
      double desired_sl = ticket == second_ticket ? stop : 0.0;
      double current_sl = PositionGetDouble(POSITION_SL);
      double current_tp = PositionGetDouble(POSITION_TP);
      if(MathAbs(current_sl - desired_sl) < point / 2.0)
         continue;
      MqlTradeRequest request = {};
      MqlTradeResult result = {};
      request.action = TRADE_ACTION_SLTP;
      request.symbol = _Symbol;
      request.position = ticket;
      request.magic = MagicNumber;
      request.sl = desired_sl;
      request.tp = current_tp;
      if(!OrderSend(request, result) || !TradeRetcodeAccepted(result.retcode))
      {
         RecordError("Gagal memperbarui SL Layer 2 ticket=" + IntegerToString((long)ticket) +
                     " retcode=" + IntegerToString((long)result.retcode), EA_ERROR_PROTECTION);
         success = false;
      }
   }
   if(success)
      SetTrackedGroupStop(type, stop);
   return success;
}

//+------------------------------------------------------------------+
bool EnsureInitialGroupStop(const ENUM_ORDER_TYPE type)
{
   if(!HasPosition(type))
      return true;

   double stop = GetTrackedGroupStop(type);
   if(stop <= 0.0)
      stop = ExistingGroupStop(type);
   if(stop > 0.0)
   {
      double buy_average, buy_lots, sell_average, sell_lots;
      GetWeightedAveragePrices(buy_average, buy_lots, sell_average, sell_lots);
      double average = type == ORDER_TYPE_BUY ? buy_average : sell_average;
      // A stop at/past BE can only have been placed by the trailing branch,
      // because the initial group SL is always below/above BE.
      if(average > 0.0 && ((type == ORDER_TYPE_BUY && stop >= average) ||
                            (type == ORDER_TYPE_SELL && stop <= average)))
         SetTrailingActive(type, true);
   }
   if(stop <= 0.0)
   {
      double latest_price = GetLatestOpenPrice(type);
      if(latest_price <= 0.0)
         return false;
      int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
      double distance = cfgInitialSlPips * NILAI_1_PIP_XAUUSD;
      stop = NormalizeDouble(type == ORDER_TYPE_BUY ? latest_price - distance : latest_price + distance, digits);
   }
   // Layer 1 has TP only. Layer 2 keeps SL even when later martingale layers open.
   if(CountPositions(type) < 2)
      return true;
   return ApplySecondLayerStop(type, stop);
}

//+------------------------------------------------------------------+
bool UpdateTrailingProtection()
{
   bool success = true;
   if(HasPosition(ORDER_TYPE_BUY))
      success = EnsureInitialGroupStop(ORDER_TYPE_BUY) && success;
   if(HasPosition(ORDER_TYPE_SELL))
      success = EnsureInitialGroupStop(ORDER_TYPE_SELL) && success;
   if(success)
      ClearRecordedError(EA_ERROR_PROTECTION);
   return success;
}

//+------------------------------------------------------------------+
bool CloseOwnedPositions(const ENUM_ORDER_TYPE type)
{
   bool success = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(!IsOwnedPosition(ticket) || PositionGetInteger(POSITION_TYPE) != type)
         continue;
      MqlTradeRequest request = {};
      MqlTradeResult result = {};
      request.action = TRADE_ACTION_DEAL;
      request.position = ticket;
      request.symbol = _Symbol;
      request.magic = MagicNumber;
      request.volume = PositionGetDouble(POSITION_VOLUME);
      request.type = type == ORDER_TYPE_BUY ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
      request.price = request.type == ORDER_TYPE_BUY ? SymbolInfoDouble(_Symbol, SYMBOL_ASK) : SymbolInfoDouble(_Symbol, SYMBOL_BID);
      request.deviation = 15;
      request.type_filling = GetFillingMode();
      request.comment = "Trailing group stop";
      if(!OrderSend(request, result) || !TradeRetcodeAccepted(result.retcode))
      {
         RecordError("Gagal tutup siklus trailing ticket=" + IntegerToString((long)ticket) +
                     " retcode=" + IntegerToString((long)result.retcode), EA_ERROR_PROTECTION);
         success = false;
      }
   }
   return success;
}

//+------------------------------------------------------------------+
void ManageGroupTrailing(const ENUM_ORDER_TYPE type)
{
   if(!HasPosition(type))
      return;
   if(!EnsureInitialGroupStop(type))
      return;

   double stop = GetTrackedGroupStop(type);
   double price = type == ORDER_TYPE_BUY ? SymbolInfoDouble(_Symbol, SYMBOL_BID) : SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   if(stop > 0.0 && ((type == ORDER_TYPE_BUY && price <= stop) || (type == ORDER_TYPE_SELL && price >= stop)))
   {
      CloseOwnedPositions(type);
      return;
   }

   double buy_average, buy_lots, sell_average, sell_lots;
   GetWeightedAveragePrices(buy_average, buy_lots, sell_average, sell_lots);
   double average = type == ORDER_TYPE_BUY ? buy_average : sell_average;
   if(average <= 0.0)
      return;
   double gain = type == ORDER_TYPE_BUY ? price - average : average - price;
   if(gain < cfgTrailingStartPips * NILAI_1_PIP_XAUUSD)
      return;

   SetTrailingActive(type, true);

   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double candidate = NormalizeDouble(type == ORDER_TYPE_BUY
                                      ? price - cfgTrailingDistancePips * NILAI_1_PIP_XAUUSD
                                      : price + cfgTrailingDistancePips * NILAI_1_PIP_XAUUSD,
                                      digits);
   // The candidate can only tighten; it never gives back an existing group SL.
   if(type == ORDER_TYPE_BUY)
      candidate = MathMax(candidate, stop);
   else
      candidate = stop > 0.0 ? MathMin(candidate, stop) : candidate;

   if(stop > 0.0 && MathAbs(candidate - stop) < cfgTrailingStepPips * NILAI_1_PIP_XAUUSD)
      return;
   if(TimeCurrent() - lastTrailingProtectionAttempt < 1)
      return;
   lastTrailingProtectionAttempt = TimeCurrent();
   if(CountPositions(type) >= 2)
      ApplySecondLayerStop(type, candidate);
}

//+------------------------------------------------------------------+
bool OpenPosition(ENUM_ORDER_TYPE type, const double requested_lot)
{
   // Standalone V6.2 uses a 3-second safety interval between entry requests.
   if(lastEntryAttemptTime > 0 && TimeCurrent() - lastEntryAttemptTime < ENTRY_SAFE_DELAY_SECONDS)
      return false;

   if(!EntryAttemptAllowed())
      return false;

   MqlTradeRequest request = {};
   MqlTradeResult result = {};
   double price = type == ORDER_TYPE_BUY
                  ? SymbolInfoDouble(_Symbol, SYMBOL_ASK)
                  : SymbolInfoDouble(_Symbol, SYMBOL_BID);
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double sl_distance = cfgInitialSlPips * NILAI_1_PIP_XAUUSD;
   bool opening_first_layer = CountPositions(type) == 0;

   double lot = NormalizeDouble(requested_lot, 2);
   lot = MathMin(lot, NormalizeDouble(cfgMaxLot, 2));
   lot = NormalizeVolume(lot);
   double sl_awal = NormalizeDouble(type == ORDER_TYPE_BUY
                                     ? price - sl_distance
                                     : price + sl_distance, digits);

   request.action = TRADE_ACTION_DEAL;
   request.symbol = _Symbol;
   request.magic = MagicNumber;
   request.volume = lot;
   request.type = type;
   request.price = price;
   // Layer 1 carries TP only; SL begins only when layer 2 exists.
   request.sl = opening_first_layer ? 0.0 : sl_awal;
   // Layer 1 only gets TP at creation. Martingale layers 2+ get TP=0.
   request.tp = opening_first_layer
                ? NormalizeDouble(type == ORDER_TYPE_BUY
                                  ? price + cfgFirstLayerTpPips * NILAI_1_PIP_XAUUSD
                                  : price - cfgFirstLayerTpPips * NILAI_1_PIP_XAUUSD, digits)
                : 0.0;
   request.deviation = 15;
   request.type_filling = GetFillingMode();
   request.comment = "RSI-Web-V6.2-Algo";

   lastEntryAttemptTime = TimeCurrent();
   ResetLastError();
   if(!OrderSend(request, result) || !TradeRetcodeAccepted(result.retcode))
   {
      if(result.retcode == TRADE_RETCODE_SERVER_DISABLES_AT)
         PauseEntryAfterServerDisable();
      else
      {
         ResetServerDisableEntryBackoff();
         RecordError("Gagal buka posisi. retcode=" + IntegerToString((long)result.retcode) +
                     " error=" + IntegerToString(GetLastError()),
                     EA_ERROR_OPEN);
      }
      return false;
   }

   ResetServerDisableEntryBackoff();
   ClearRecordedError(EA_ERROR_OPEN);

   Print("OPEN ", type == ORDER_TYPE_BUY ? "BUY" : "SELL",
         " | lot=", request.volume, " | price=", price,
         " | SL=", sl_awal, " | TP=", request.tp,
         " | deal=", result.deal);

   // Layer 1 keeps TP only. Layer 2 owns SL permanently; later layers only
   // recalculate its price from the newest entry, never its ticket ownership.
   if(CountPositions(type) >= 2)
      ApplySecondLayerStop(type, sl_awal);
   UpdateFirstLayerTP(type);
   return true;
}

//+------------------------------------------------------------------+
bool HasOwnedPendingOrders()
{
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0 || OrderGetString(ORDER_SYMBOL) != _Symbol ||
         (ulong)OrderGetInteger(ORDER_MAGIC) != MagicNumber)
         continue;

      ENUM_ORDER_TYPE type = (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      if(type == ORDER_TYPE_BUY_LIMIT || type == ORDER_TYPE_SELL_LIMIT ||
         type == ORDER_TYPE_BUY_STOP || type == ORDER_TYPE_SELL_STOP ||
         type == ORDER_TYPE_BUY_STOP_LIMIT || type == ORDER_TYPE_SELL_STOP_LIMIT)
         return true;
   }
   return false;
}

//+------------------------------------------------------------------+
bool DeleteOwnedPendingOrders()
{
   bool success = true;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong ticket = OrderGetTicket(i);
      if(ticket == 0 || OrderGetString(ORDER_SYMBOL) != _Symbol ||
         (ulong)OrderGetInteger(ORDER_MAGIC) != MagicNumber)
         continue;

      ENUM_ORDER_TYPE type = (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      if(type != ORDER_TYPE_BUY_LIMIT && type != ORDER_TYPE_SELL_LIMIT &&
         type != ORDER_TYPE_BUY_STOP && type != ORDER_TYPE_SELL_STOP &&
         type != ORDER_TYPE_BUY_STOP_LIMIT && type != ORDER_TYPE_SELL_STOP_LIMIT)
         continue;

      MqlTradeRequest request = {};
      MqlTradeResult result = {};
      request.action = TRADE_ACTION_REMOVE;
      request.order = ticket;
      request.symbol = _Symbol;
      request.magic = MagicNumber;
      if(!OrderSend(request, result) || !TradeRetcodeAccepted(result.retcode))
      {
         RecordError("Gagal hapus pending ticket=" + IntegerToString((long)ticket) +
                     " retcode=" + IntegerToString((long)result.retcode),
                     EA_ERROR_STOP);
         success = false;
      }
   }
   return success;
}

//+------------------------------------------------------------------+
bool CloseAllOwnedPositions()
{
   bool success = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(!IsOwnedPosition(ticket))
         continue;

      ENUM_ORDER_TYPE current_type = (ENUM_ORDER_TYPE)PositionGetInteger(POSITION_TYPE);
      double volume = PositionGetDouble(POSITION_VOLUME);
      MqlTradeRequest request = {};
      MqlTradeResult result = {};
      request.action = TRADE_ACTION_DEAL;
      request.position = ticket;
      request.symbol = _Symbol;
      request.magic = MagicNumber;
      request.volume = volume;
      request.type = current_type == ORDER_TYPE_BUY ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
      request.price = request.type == ORDER_TYPE_BUY
                      ? SymbolInfoDouble(_Symbol, SYMBOL_ASK)
                      : SymbolInfoDouble(_Symbol, SYMBOL_BID);
      request.deviation = 15;
      request.type_filling = GetFillingMode();
      request.comment = "WebUI Close All";

      if(!OrderSend(request, result) || !TradeRetcodeAccepted(result.retcode))
      {
         RecordError("Gagal tutup posisi ticket=" + IntegerToString((long)ticket) +
                     " retcode=" + IntegerToString((long)result.retcode),
                     EA_ERROR_STOP);
         success = false;
      }
   }
   return success;
}

//+------------------------------------------------------------------+
bool TradeRetcodeAccepted(const uint retcode)
{
   return retcode == TRADE_RETCODE_DONE ||
          retcode == TRADE_RETCODE_DONE_PARTIAL ||
          retcode == TRADE_RETCODE_PLACED ||
          retcode == TRADE_RETCODE_NO_CHANGES;
}

//+------------------------------------------------------------------+
ENUM_ORDER_TYPE_FILLING GetFillingMode()
{
   long filling = SymbolInfoInteger(_Symbol, SYMBOL_FILLING_MODE);
   if((filling & SYMBOL_FILLING_FOK) == SYMBOL_FILLING_FOK)
      return ORDER_FILLING_FOK;
   if((filling & SYMBOL_FILLING_IOC) == SYMBOL_FILLING_IOC)
      return ORDER_FILLING_IOC;
   return ORDER_FILLING_RETURN;
}

//+------------------------------------------------------------------+
double FloatingProfit()
{
   double profit = 0.0;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(IsOwnedPosition(ticket))
         profit += PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
   }
   return profit;
}

//+------------------------------------------------------------------+
bool DailyProfits(double &ea_profit, double &account_profit, string &profit_date)
{
   ea_profit = 0.0;
   account_profit = 0.0;
   profit_date = "";

   datetime server_now = TimeTradeServer();
   if(server_now <= 0)
      server_now = TimeCurrent();
   if(server_now <= 0)
      return false;

   MqlDateTime date_parts;
   ZeroMemory(date_parts);
   if(!TimeToStruct(server_now, date_parts))
      return false;

   profit_date = StringFormat("%04d-%02d-%02d", date_parts.year, date_parts.mon, date_parts.day);
   date_parts.hour = 0;
   date_parts.min = 0;
   date_parts.sec = 0;
   datetime day_start = StructToTime(date_parts);
   if(day_start <= 0 || !HistorySelect(day_start, server_now))
      return false;

   int deal_total = HistoryDealsTotal();
   for(int i = 0; i < deal_total; i++)
   {
      ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0)
         continue;

      ENUM_DEAL_TYPE deal_type = (ENUM_DEAL_TYPE)HistoryDealGetInteger(ticket, DEAL_TYPE);
      if(deal_type != DEAL_TYPE_BUY && deal_type != DEAL_TYPE_SELL)
         continue;

      double deal_profit = HistoryDealGetDouble(ticket, DEAL_PROFIT) +
                           HistoryDealGetDouble(ticket, DEAL_SWAP) +
                           HistoryDealGetDouble(ticket, DEAL_COMMISSION) +
                           HistoryDealGetDouble(ticket, DEAL_FEE);
      account_profit += deal_profit;

      if(HistoryDealGetString(ticket, DEAL_SYMBOL) == _Symbol &&
         (ulong)HistoryDealGetInteger(ticket, DEAL_MAGIC) == MagicNumber)
      {
         ea_profit += deal_profit;
      }
   }
   return true;
}

//+------------------------------------------------------------------+
void RefreshDailyProfitCache()
{
   ulong now_ms = GetTickCount64();
   if(lastDailyProfitRefreshMs != 0 && now_ms - lastDailyProfitRefreshMs < DAILY_PROFIT_REFRESH_MS)
      return;
   lastDailyProfitRefreshMs = now_ms;
   cachedDailyProfitAvailable = DailyProfits(
      cachedDailyProfit,
      cachedAccountDailyProfit,
      cachedDailyProfitDate
   );
}

//+------------------------------------------------------------------+
bool PostStatus(string &response)
{
   int buy_count = CountPositions(ORDER_TYPE_BUY);
   int sell_count = CountPositions(ORDER_TYPE_SELL);
   RefreshDailyProfitCache();
   string daily_profit_json = cachedDailyProfitAvailable ? DoubleToString(cachedDailyProfit, 2) : "null";
   string account_daily_profit_json = cachedDailyProfitAvailable ? DoubleToString(cachedAccountDailyProfit, 2) : "null";
   double telemetry_rsi = liveRsiAvailable ? liveRsi : currentRsi;
   string body = "{" +
      "\"ea_id\":\"" + JsonEscape(EaId) + "\"," +
      "\"client_id\":\"" + JsonEscape(clientInstanceId) + "\"," +
      "\"ea_version\":\"2.543\"," +
      "\"strategy_type\":\"trailing\"," +
      "\"symbol\":\"" + JsonEscape(_Symbol) + "\"," +
      "\"account_login\":\"" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "\"," +
      "\"algo_enabled\":" + (cfgAlgoEnabled ? "true" : "false") + "," +
      "\"bid\":" + DoubleToString(SymbolInfoDouble(_Symbol, SYMBOL_BID), _Digits) + "," +
      "\"ask\":" + DoubleToString(SymbolInfoDouble(_Symbol, SYMBOL_ASK), _Digits) + "," +
      "\"rsi\":" + DoubleToString(telemetry_rsi, 2) + "," +
      "\"buy_positions\":" + IntegerToString(buy_count) + "," +
      "\"sell_positions\":" + IntegerToString(sell_count) + "," +
      "\"total_positions\":" + IntegerToString(buy_count + sell_count) + "," +
      "\"floating_profit\":" + DoubleToString(FloatingProfit(), 2) + "," +
      "\"account_floating_profit\":" + DoubleToString(AccountInfoDouble(ACCOUNT_PROFIT), 2) + "," +
      "\"account_balance\":" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) + "," +
      "\"account_currency\":\"" + JsonEscape(AccountInfoString(ACCOUNT_CURRENCY)) + "\"," +
      "\"daily_profit\":" + daily_profit_json + "," +
      "\"daily_profit_date\":\"" + JsonEscape(cachedDailyProfitDate) + "\"," +
      "\"account_daily_profit\":" + account_daily_profit_json + "," +
      "\"account_profit_date\":\"" + JsonEscape(cachedDailyProfitDate) + "\"," +
      "\"config_revision\":" + IntegerToString(lastConfigRevision) + "," +
      "\"last_error\":\"" + JsonEscape(lastErrorMessage) + "\"" +
      "}";
   return HttpRequest("POST", "/api/ea/status", body, response);
}

//+------------------------------------------------------------------+
bool HttpRequest(const string method, const string path, const string body, string &response)
{
   string base = ApiBaseUrl;
   if(StringLen(base) > 0 && StringSubstr(base, StringLen(base) - 1, 1) == "/")
      base = StringSubstr(base, 0, StringLen(base) - 1);
   string url = base + path;
   string headers = "Content-Type: application/json\r\nAccept: application/json\r\n";
   if(StringLen(ApiToken) > 0)
      headers += "Authorization: Bearer " + ApiToken + "\r\n";
   char request_data[];
   char result_data[];
   string result_headers = "";

   if(body != "")
   {
      int copied = StringToCharArray(body, request_data, 0, WHOLE_ARRAY, CP_UTF8);
      if(copied > 0)
         ArrayResize(request_data, copied - 1);
   }
   else
   {
      ArrayResize(request_data, 0);
   }

   ResetLastError();
   int status = WebRequest(method, url, headers, ApiTimeoutMs,
                           request_data, result_data, result_headers);
   response = CharArrayToString(result_data, 0, WHOLE_ARRAY, CP_UTF8);
   if(status < 200 || status >= 300)
   {
      RecordError("HTTP " + IntegerToString(status) +
                  " ke " + path + " | MQL error=" + IntegerToString(GetLastError()),
                  EA_ERROR_API);
      return false;
   }
   ClearRecordedError(EA_ERROR_API);
   return true;
}

//+------------------------------------------------------------------+
string JsonEscape(string value)
{
   StringReplace(value, "\\", "\\\\");
   StringReplace(value, "\"", "\\\"");
   StringReplace(value, "\r", "\\r");
   StringReplace(value, "\n", "\\n");
   return value;
}

//+------------------------------------------------------------------+
bool JsonGetRaw(const string json, const string key, string &value)
{
   string token = "\"" + key + "\"";
   int key_pos = StringFind(json, token);
   if(key_pos < 0)
      return false;
   int colon = StringFind(json, ":", key_pos + StringLen(token));
   if(colon < 0)
      return false;

   int start = colon + 1;
   int length = StringLen(json);
   while(start < length && StringGetCharacter(json, start) <= 32)
      start++;
   if(start >= length)
      return false;

   if(StringGetCharacter(json, start) == 34)
   {
      start++;
      int finish = start;
      bool escaped = false;
      while(finish < length)
      {
         ushort current = (ushort)StringGetCharacter(json, finish);
         if(current == 34 && !escaped)
         {
            value = StringSubstr(json, start, finish - start);
            return true;
         }
         if(current == 92 && !escaped)
            escaped = true;
         else
            escaped = false;
         finish++;
      }
      return false;
   }

   int finish = start;
   while(finish < length)
   {
      ushort current = (ushort)StringGetCharacter(json, finish);
      if(current == 44 || current == 125 || current == 13 || current == 10)
         break;
      finish++;
   }
   value = StringSubstr(json, start, finish - start);
   while(StringLen(value) > 0 && StringGetCharacter(value, StringLen(value) - 1) <= 32)
      value = StringSubstr(value, 0, StringLen(value) - 1);
   return StringLen(value) > 0;
}

//+------------------------------------------------------------------+
bool JsonGetString(const string json, const string key, string &value)
{
   return JsonGetRaw(json, key, value);
}

//+------------------------------------------------------------------+
bool JsonGetLong(const string json, const string key, long &value)
{
   string raw;
   if(!JsonGetRaw(json, key, raw))
      return false;
   value = StringToInteger(raw);
   return true;
}

//+------------------------------------------------------------------+
bool JsonGetDouble(const string json, const string key, double &value)
{
   string raw;
   if(!JsonGetRaw(json, key, raw))
      return false;
   value = StringToDouble(raw);
   return true;
}

//+------------------------------------------------------------------+
bool JsonGetBool(const string json, const string key, bool &value)
{
   string raw;
   if(!JsonGetRaw(json, key, raw))
      return false;
   if(raw == "true")
   {
      value = true;
      return true;
   }
   if(raw == "false")
   {
      value = false;
      return true;
   }
   return false;
}
