//+------------------------------------------------------------------+
//| CriticalConsensusStandalone.mq5                                  |
//| Self-contained critical-consensus expert: the whole pipeline      |
//| (agents -> certainty gate -> work mode -> Telegram approval)      |
//| lives inside MT5. No Python, no local API, no extra installs.     |
//|                                                                   |
//| DEMO-ONLY: the EA refuses to trade on a real account.             |
//| Setup: copy to MQL5/Experts, compile (F7), add                    |
//| https://api.telegram.org to the WebRequest allowlist, attach      |
//| to an EURUSD chart, send /start to your bot once.                 |
//+------------------------------------------------------------------+
#property strict
#property version   "1.00"
#property description "Critical Consensus for MT5, standalone. Demo-only. Telegram approvals."

// No #include of the standard library: the EA uses raw MqlTradeRequest/OrderSend,
// so it compiles even on a fresh terminal without the extracted Include tree
// (same style as the repo's bridge EA).

//--- work mode: operator's choice
enum WorkModeEnum { MODE_CONFIRMATION = 0, MODE_AUTONOMOUS = 1 };
input WorkModeEnum WorkMode          = MODE_CONFIRMATION; // Work mode: confirmation or autonomous
input string TelegramToken         = "";            // Telegram bot token from @BotFather
input string TelegramChatID        = "";            // Chat ID (leave empty: auto-detected after you /start the bot)
input double LotSize               = 0.01;          // Fixed lot only when risk sizing is disabled
input bool   UseRiskBasedSizing    = true;          // Size from equity and stop distance
input double RiskPercentPerTrade   = 0.50;          // Planned stop loss as % of equity; hard cap 1%
input double MaxAutonomousLot      = 0.05;          // Autonomous mode lot cap
input int    SlPips                = 20;            // Initial stop loss, true pips (not points)
input int    TakeProfitPips        = 0;             // 0 = no fixed TP; trailing stop manages exit
input int    BreakEvenAfterPips    = 10;            // Move SL beyond entry after favourable move
input int    TrailStartPips        = 20;            // Start trailing after favourable move
input int    TrailDistancePips     = 10;            // Keep SL this far behind market
input int    MaxTradesPerDay       = 3;             // Max trades per day (demo safety)
input int    MaxSpreadPoints       = 30;            // Max spread, points
input int    FastMaPeriod          = 12;            // Trend agent: fast EMA
input int    SlowMaPeriod          = 26;            // Trend agent: slow EMA
input int    RsiPeriod             = 14;            // Momentum agent: RSI period
input int    AtrPeriod             = 14;            // Volatility agent: ATR period
input long   MagicNumber           = 8172027;       // Magic number
input int    ApprovalTimeoutMin    = 5;             // Confirmation mode: approval timeout, minutes

//--- indicator handles
int hFastMa = INVALID_HANDLE, hSlowMa = INVALID_HANDLE, hRsi = INVALID_HANDLE, hAtr = INVALID_HANDLE;

// MT5 quotes use points; on 5/3-digit FX symbols one pip is 10 points.
double PipSize()
{
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   if(point <= 0.0) return 0.0;
   return point * ((digits == 3 || digits == 5) ? 10.0 : 1.0);
}

int VolumeDigits(const double step)
{
   for(int digits = 0; digits <= 8; digits++)
      if(MathAbs(NormalizeDouble(step, digits) - step) < 0.00000001)
         return digits;
   return 8;
}

double CalculateOrderVolume(const double stop_distance, string &reason)
{
   if(!UseRiskBasedSizing) return LotSize;
   if(RiskPercentPerTrade <= 0.0 || RiskPercentPerTrade > 1.0)
   { reason = "risk percent must be > 0 and <= 1.0"; return 0.0; }

   double tick_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   double tick_value = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE_LOSS);
   if(tick_value <= 0.0) tick_value = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double minvol = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxvol = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);

   if(stop_distance <= 0.0 || tick_size <= 0.0 || tick_value <= 0.0 ||
      step <= 0.0 || minvol <= 0.0 || maxvol < minvol || equity <= 0.0)
   { reason = "invalid broker tick/volume specification or equity"; return 0.0; }

   double loss_per_lot = (stop_distance / tick_size) * tick_value;
   double risk_budget = equity * RiskPercentPerTrade / 100.0;
   if(loss_per_lot <= 0.0 || risk_budget <= 0.0)
   { reason = "invalid calculated risk budget"; return 0.0; }

   double raw_volume = risk_budget / loss_per_lot;
   double volume = MathFloor(raw_volume / step + 0.000000001) * step;
   if(volume < minvol - step * 0.000001)
   { reason = "risk budget is below broker minimum lot; trade refused"; return 0.0; }
   if(volume > maxvol) volume = maxvol;
   volume = NormalizeDouble(volume, VolumeDigits(step));

   if(volume < minvol || volume > maxvol ||
      MathAbs(volume / step - MathRound(volume / step)) > 0.000001)
   { reason = "risk-sized volume violates broker constraints"; return 0.0; }
   return volume;
}

//--- telegram state
string bot_username = "";
bool   chat_known   = false;
bool   telegram_warned = false;

//--- confirmation-mode pending state
bool     pending_active   = false;
long     pending_msg_id   = 0;
string   pending_direction = "";
double   pending_sl = 0.0, pending_tp = 0.0, pending_volume = 0.0;
datetime pending_expires  = 0;

//--- safety counters
int    trades_today   = 0;
int    trades_day     = -1;
datetime last_bar_time = 0;

//+------------------------------------------------------------------+
//| Minimal JSON helpers (trusted Telegram API responses only)        |
//+------------------------------------------------------------------+
string JsonSubstring(const string json, const string key)
{
   string needle = "\"" + key + "\":";
   int p = StringFind(json, needle);
   if(p < 0) return "";
   p += StringLen(needle);
   while(p < StringLen(json) && (StringGetCharacter(json, p) == ' ' || StringGetCharacter(json, p) == '\n'))
      p++;
   int e = p;
   while(e < StringLen(json))
   {
      ushort c = StringGetCharacter(json, e);
      if(c == ',' || c == '}' || c == ']') break;
      e++;
   }
   return StringSubstr(json, p, e - p);
}

string JsonString(const string json, const string key)
{
   string raw = JsonSubstring(json, key);
   StringTrimLeft(raw); StringTrimRight(raw);
   if(StringLen(raw) >= 2 && StringGetCharacter(raw, 0) == '"')
      return StringSubstr(raw, 1, StringLen(raw) - 2);
   return "";
}

long JsonLong(const string json, const string key)
{
   string raw = JsonSubstring(json, key);
   StringTrimLeft(raw); StringTrimRight(raw);
   return StringToInteger(raw);
}

string JsonEscapeText(const string text)
{
   // Newlines inside a JSON string value must be escaped, not literal.
   string out = text;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   StringReplace(out, "\n", "\\n");
   return out;
}

string JsonChatId(const string json)
{
   // Find the "chat" object first: "from".id appears earlier in Telegram payloads.
   int p = StringFind(json, "\"chat\":");
   if(p < 0) return "";
   int id_pos = StringFind(json, "\"id\":", p);
   if(id_pos < 0) return "";
   int e = id_pos + 5;
   while(e < StringLen(json) && StringGetCharacter(json, e) == ' ') e++;   // tolerate spaces
   int start = e;
   while(e < StringLen(json))
   {
      ushort c = StringGetCharacter(json, e);
      if(c < '0' || c > '9') break;
      e++;
   }
   return StringSubstr(json, start, e - start);
}

//+------------------------------------------------------------------+
//| Telegram transport (WebRequest must allow https://api.telegram.org)|
//+------------------------------------------------------------------+
bool TelegramCall(const string method, const string json_payload, string &response)
{
   string url = "https://api.telegram.org/bot" + TelegramToken + "/" + method;
   string headers = "Content-Type: application/json\r\n";
   char data[], result[];
   StringToCharArray(json_payload, data, 0, StringLen(json_payload), CP_UTF8);
   ResetLastError();
   int code = WebRequest("POST", url, headers, 10000, data, result, response);
   if(code == -1)
   {
      int err = GetLastError();
      if(!telegram_warned)
      {
         Print("TELEGRAM BLOCKED: add https://api.telegram.org to MT5 WebRequest allowlist",
               " (Tools -> Options -> Expert Advisors). error=", err);
         telegram_warned = true;
      }
      return false;
   }
   if(code != 200)
   {
      Print("TELEGRAM HTTP ", code, "; fail closed.");
      return false;
   }
   response = CharArrayToString(result, 0, -1, CP_UTF8);
   return true;
}

//+------------------------------------------------------------------+
//| Chat setup: auto-detect chat_id after the operator sends /start   |
//+------------------------------------------------------------------+
void SetupChat()
{
   string response = "";
   if(!TelegramCall("getMe", "{}", response))
      return;
   bot_username = JsonString(response, "username");
   if(StringLen(TelegramChatID) > 0)
   {
      chat_known = true;
      Print("TELEGRAM: chat_id set from input: ", TelegramChatID);
      return;
   }
   if(!TelegramCall("getUpdates", "{\"timeout\":0,\"allowed_updates\":[\"message\"]}", response))
      return;
   string chat_id = JsonChatId(response);
   if(StringLen(chat_id) > 0)
   {
      chat_known = true;
      Print("TELEGRAM: chat_id auto-detected: ", chat_id,
            ". (Operator already sent /start; fixed in input for next launches.)");
   }
   else if(StringLen(bot_username) > 0)
      Print("TELEGRAM: open chat with @", bot_username, " and send /start once to enable approvals.");
}

void TelegramSend(const string text)
{
   if(!chat_known) return;
   string response = "";
   string payload = "{\"chat_id\":\"" + TelegramChatID + "\",\"text\":\"" + JsonEscapeText(text) + "\"}";
   TelegramCall("sendMessage", payload, response);
}

//+------------------------------------------------------------------+
//| Safety: demo-only, per-day caps                                   |
//+------------------------------------------------------------------+
bool SafetyOk(string &reason)
{
   if(AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO)
   { reason = "account is not a DEMO account; this EA never trades live"; return false; }
   MqlDateTime day;
   TimeToStruct(TimeCurrent(), day);
   if(trades_day != day.day_of_year) { trades_day = day.day_of_year; trades_today = 0; }
   if(trades_today >= MaxTradesPerDay)
   { reason = "daily trade cap reached"; return false; }
   long spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   if(spread > MaxSpreadPoints)
   { reason = "spread too high: " + IntegerToString(spread); return false; }
   if(PositionsTotal() > 0)
   { reason = "one position at a time (demo safety)"; return false; }
   return true;
}

//+------------------------------------------------------------------+
//| Agents: each returns APPROVE / VETO for its own direction bias.   |
//| Certainty gate: conjunction — every agent must APPROVE.           |
//+------------------------------------------------------------------+
string AgentVerdict(const string agent, const bool agree, const string reason,
                    string &verdicts_log)
{
   string verdict = agree ? "APPROVE" : "VETO";
   verdicts_log += agent + "=" + verdict + (agree ? "" : " (" + reason + ")") + "; ";
   return verdict;
}

bool CertaintyGate(const double &fast[], const double &slow[], const double &rsi[],
                   const double &atr[], const double atr_baseline, string &direction, string &log)
{
   // CopyBuffer static arrays are oldest-first. Compare two completed bars.
   // A bearish EMA alignment is not a SELL signal if the fast EMA is already turning up.
   bool up = fast[1] > slow[1] && fast[1] > fast[0] && slow[1] >= slow[0];
   bool down = fast[1] < slow[1] && fast[1] < fast[0] && slow[1] <= slow[0];
   if(!up && !down)
   {
      direction = "";
      log = "TrendAgent=VETO (EMA direction/slope not aligned)";
      Print("CERTAINTY GATE: ", log);
      return false;
   }
   direction = up ? "BUY" : "SELL";

   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   double gap = MathAbs(fast[1] - slow[1]);
   bool trend_ok = gap >= MathMax(atr[1] * 0.10, point * 3.0);
   string v1 = AgentVerdict("TrendAgent", trend_ok,
                            "EMA gap below 0.10 ATR / 3 points", log);

   bool rsi_ok = (up && rsi[1] > 45.0 && rsi[1] < 70.0) ||
                 (down && rsi[1] > 30.0 && rsi[1] < 55.0);
   string v2 = AgentVerdict("MomentumAgent", rsi_ok,
                            "RSI " + DoubleToString(rsi[1], 1), log);

   long spread_points = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   double spread_price = spread_points * point;
   bool atr_ok = atr[1] > MathMax(point * 5.0, spread_price * 2.0) &&
                 atr_baseline > 0.0 && atr[1] < atr_baseline * 2.5;
   string v3 = AgentVerdict("VolatilityAgent", atr_ok,
                            "ATR too low or abnormal", log);

   double open_i = iOpen(_Symbol, PERIOD_CURRENT, 1);
   double close_i = iClose(_Symbol, PERIOD_CURRENT, 1);
   bool candle_ok = (up && close_i > open_i) || (down && close_i < open_i);
   string v4 = AgentVerdict("PriceActionAgent", candle_ok,
                            "last completed candle against direction", log);

   MqlDateTime tm;
   TimeToStruct(TimeCurrent(), tm);
   bool session_ok = !(tm.hour >= 23 || tm.hour < 1);
   string v5 = AgentVerdict("SessionAgent", session_ok, "rollover hours", log);

   Print("CERTAINTY GATE: ", log);
   return (v1 == "APPROVE" && v2 == "APPROVE" && v3 == "APPROVE" &&
           v4 == "APPROVE" && v5 == "APPROVE");
}

//+------------------------------------------------------------------+
//| Order placement (demo only)                                       |
//+------------------------------------------------------------------+
bool PlaceDemoOrder(const string direction, const double volume, string &card_text)
{
   string reason = "";
   if(!SafetyOk(reason))
   { Print("BLOCKED: ", reason); return false; }

   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   double pip = PipSize();
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) { Print("BLOCKED: no tick"); return false; }
   if(point <= 0.0 || pip <= 0.0) { Print("BLOCKED: invalid symbol point/pip size"); return false; }
   double price = (direction == "BUY") ? tick.ask : tick.bid;
   double sl = (direction == "BUY") ? price - SlPips * pip : price + SlPips * pip;
   double tp = 0.0;
   if(TakeProfitPips > 0)
      tp = (direction == "BUY") ? price + TakeProfitPips * pip : price - TakeProfitPips * pip;
   sl = NormalizeDouble(sl, digits);
   if(tp > 0.0) tp = NormalizeDouble(tp, digits);

   // Volume constraints, same as the repo's bridge EA.
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   double minvol = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   double maxvol = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   if(step <= 0 || volume < minvol || volume > maxvol || MathAbs(volume / step - MathRound(volume / step)) > 0.000001)
   { Print("BLOCKED: broker volume constraints not satisfied."); return false; }

   MqlTradeRequest request = {};
   MqlTradeCheckResult check = {};
   MqlTradeResult result = {};
   request.action = TRADE_ACTION_DEAL;
   request.magic = MagicNumber;
   request.symbol = _Symbol;
   request.volume = volume;
   request.type = (direction == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   request.price = NormalizeDouble(price, digits);
   request.sl = sl;
   request.tp = tp;
   request.deviation = MaxSpreadPoints;
   request.type_time = ORDER_TIME_GTC;
   request.comment = "CC-standalone";
   if(!OrderCheck(request, check))
   { Print("BLOCKED: OrderCheck failed; retcode=", check.retcode, " ", check.comment); return false; }
   ResetLastError();
   if(!OrderSend(request, result) ||
      (result.retcode != TRADE_RETCODE_DONE && result.retcode != TRADE_RETCODE_PLACED))
   { Print("ORDER NOT CONFIRMED: sent=", result.retcode, " error=", GetLastError()); return false; }

   trades_today++;
   string tp_text = (tp > 0.0) ? DoubleToString(tp, digits) : "TRAILING EXIT";
   card_text = "✅ OPENED " + direction + " " + DoubleToString(volume, 3) + " lot " + _Symbol +
               " @ " + DoubleToString(price, digits) +
               "\nSL " + DoubleToString(sl, digits) + "  TP " + tp_text +
               "\nRisk budget " + DoubleToString(RiskPercentPerTrade, 2) + "% equity" +
               "\nDemo money. Trades today: " + IntegerToString(trades_today) + "/" + IntegerToString(MaxTradesPerDay);
   return true;
}

//+------------------------------------------------------------------+
//| Work modes                                                        |
//+------------------------------------------------------------------+
void HandleSignal(const double &fast[], const double &slow[], const double &rsi[],
                  const double &atr[], const double atr_baseline)
{
   string direction = "", log = "";
   if(!CertaintyGate(fast, slow, rsi, atr, atr_baseline, direction, log))
      return;

   string reason = "";
   if(!SafetyOk(reason))
   { Print("BLOCKED: ", reason); return; }

   double pip = PipSize();
   MqlTick tick;
   if(pip <= 0.0 || !SymbolInfoTick(_Symbol, tick))
   { Print("BLOCKED: pip size or quote unavailable"); return; }
   double stop_distance = SlPips * pip;
   double volume = CalculateOrderVolume(stop_distance, reason);
   if(volume <= 0.0)
   { Print("BLOCKED: ", reason); return; }

   if(WorkMode == MODE_AUTONOMOUS)
   {
      if(volume > MaxAutonomousLot)
      { Print("BLOCKED: risk-sized lot ", DoubleToString(volume, 3),
              " above autonomous cap ", DoubleToString(MaxAutonomousLot, 2)); return; }
      string card = "";
      if(PlaceDemoOrder(direction, volume, card))
         TelegramSend("🤖 AUTONOMOUS (demo)\n" + card);
      return;
   }

   if(!chat_known)
   { Print("BLOCKED: Telegram chat is not configured — confirmation mode requires the phone."); return; }

   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   double price = (direction == "BUY") ? tick.ask : tick.bid;
   double sl = (direction == "BUY") ? price - stop_distance : price + stop_distance;
   double tp = 0.0;
   if(TakeProfitPips > 0)
      tp = (direction == "BUY") ? price + TakeProfitPips * pip : price - TakeProfitPips * pip;
   pending_direction = direction;
   pending_sl = NormalizeDouble(sl, digits);
   pending_tp = (tp > 0.0) ? NormalizeDouble(tp, digits) : 0.0;
   pending_volume = volume;
   pending_expires = TimeCurrent() + ApprovalTimeoutMin * 60;
   pending_active = true;

   string target_text = (pending_tp > 0.0) ? DoubleToString(pending_tp, digits) : "TRAILING EXIT";
   string card = "🤖 Signal " + _Symbol + " — needs your approval\n" +
                 "Direction: " + direction + "  Lot: " + DoubleToString(volume, 3) +
                 "\nSL: " + DoubleToString(pending_sl, digits) + "  TP: " + target_text +
                 "\nRisk budget: " + DoubleToString(RiskPercentPerTrade, 2) + "% equity" +
                 "\nGate: " + log +
                 "\nDemo money, " + IntegerToString(ApprovalTimeoutMin) + " min to answer";
   string response = "";
   string payload = "{\"chat_id\":\"" + TelegramChatID + "\",\"text\":\"" + JsonEscapeText(card) + "\"," 
                    "\"reply_markup\":{\"inline_keyboard\":[[{\"text\":\"✅ Approve\",\"callback_data\":\"approve\"},"
                    "{\"text\":\"❌ Reject\",\"callback_data\":\"reject\"}]]}}";
   if(TelegramCall("sendMessage", payload, response))
   {
      pending_msg_id = JsonLong(response, "message_id");
      Print("CONFIRMATION REQUESTED on Telegram; message_id=", pending_msg_id);
   }
   else
   {
      pending_active = false;
      Print("BLOCKED: Telegram unavailable; signal discarded.");
   }
}

// Tighten the broker-side stop only in the profitable direction; never widen risk.
void ManageOpenPositions()
{
   double point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   double pip = PipSize();
   int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   if(point <= 0.0 || pip <= 0.0) return;

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick)) return;

   long stops_points = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   long freeze_points = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_FREEZE_LEVEL);
   double min_distance = MathMax((double)stops_points, (double)freeze_points) * point;
   double spread_buffer = MathMax(tick.ask - tick.bid, pip);

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      string symbol = PositionGetString(POSITION_SYMBOL);
      long magic = PositionGetInteger(POSITION_MAGIC);
      if(symbol != _Symbol || magic != MagicNumber) continue;

      ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double old_sl = PositionGetDouble(POSITION_SL);
      double old_tp = PositionGetDouble(POSITION_TP);
      if(entry <= 0.0) continue;

      double favourable_pips = 0.0;
      double candidate_sl = old_sl;
      if(type == POSITION_TYPE_BUY)
      {
         favourable_pips = (tick.bid - entry) / pip;
         if(favourable_pips >= BreakEvenAfterPips)
         {
            double breakeven = entry + spread_buffer;
            if(candidate_sl <= 0.0 || breakeven > candidate_sl) candidate_sl = breakeven;
         }
         if(favourable_pips >= TrailStartPips)
         {
            double trailing = tick.bid - TrailDistancePips * pip;
            if(candidate_sl <= 0.0 || trailing > candidate_sl) candidate_sl = trailing;
         }
         if(candidate_sl <= 0.0 || (old_sl > 0.0 && candidate_sl <= old_sl + point * 0.5))
            continue;
         candidate_sl = NormalizeDouble(candidate_sl, digits);
         if(candidate_sl >= tick.bid - min_distance) continue;
      }
      else if(type == POSITION_TYPE_SELL)
      {
         favourable_pips = (entry - tick.ask) / pip;
         if(favourable_pips >= BreakEvenAfterPips)
         {
            double breakeven = entry - spread_buffer;
            if(candidate_sl <= 0.0 || breakeven < candidate_sl) candidate_sl = breakeven;
         }
         if(favourable_pips >= TrailStartPips)
         {
            double trailing = tick.ask + TrailDistancePips * pip;
            if(candidate_sl <= 0.0 || trailing < candidate_sl) candidate_sl = trailing;
         }
         if(candidate_sl <= 0.0 || (old_sl > 0.0 && candidate_sl >= old_sl - point * 0.5))
            continue;
         candidate_sl = NormalizeDouble(candidate_sl, digits);
         if(candidate_sl <= tick.ask + min_distance) continue;
      }
      else continue;

      MqlTradeRequest request = {};
      MqlTradeResult result = {};
      request.action = TRADE_ACTION_SLTP;
      request.position = ticket;
      request.symbol = symbol;
      request.sl = candidate_sl;
      request.tp = old_tp;

      ResetLastError();
      if(!OrderSend(request, result) || result.retcode != TRADE_RETCODE_DONE)
      {
         Print("TRAILING STOP NOT CONFIRMED: ticket=", ticket,
               " retcode=", result.retcode, " error=", GetLastError(),
               " comment=", result.comment);
         continue;
      }
      Print("PROTECTIVE STOP MOVED: ticket=", ticket,
            " SL=", DoubleToString(candidate_sl, digits),
            " favourable_pips=", DoubleToString(favourable_pips, 1));
   }
}

void PollApproval()
{
   string response = "";
   if(!TelegramCall("getUpdates", "{\"timeout\":0,\"allowed_updates\":[\"callback_query\"]}", response))
      return;
   if(StringFind(response, "\"callback_query\"") < 0) return;
   if(StringLen(TelegramChatID) == 0) return;
   bool approved = (StringFind(response, "\"data\":\"approve\"") >= 0);
   bool rejected = (StringFind(response, "\"data\":\"reject\"") >= 0);
   if(!approved && !rejected) return;

   pending_active = false; // consume whichever answer arrived
   if(rejected)
   {
      TelegramSend("❌ REJECTED — nothing happens.");
      Print("Operator REJECTED the signal.");
      return;
   }
   string card = "";
   if(PlaceDemoOrder(pending_direction, pending_volume, card))
      TelegramSend(card);
}

//+------------------------------------------------------------------+
//| Expert lifecycle                                                  |
//+------------------------------------------------------------------+
int OnInit()
{
   if(AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO)
      Print("WARNING: this is NOT a demo account. The EA will refuse to trade.");
   if(StringLen(TelegramToken) == 0)
      Print("WARNING: TelegramToken is empty — confirmation mode cannot ask; autonomous will only log.");
   if(WorkMode == MODE_CONFIRMATION && LotSize > MaxAutonomousLot)
      Print("NOTE: LotSize ", DoubleToString(LotSize, 2), " > autonomous cap ",
            DoubleToString(MaxAutonomousLot, 2), " — autonomous mode would block this size.");

   hFastMa = iMA(_Symbol, PERIOD_CURRENT, FastMaPeriod, 0, MODE_EMA, PRICE_CLOSE);
   hSlowMa = iMA(_Symbol, PERIOD_CURRENT, SlowMaPeriod, 0, MODE_EMA, PRICE_CLOSE);
   hRsi    = iRSI(_Symbol, PERIOD_CURRENT, RsiPeriod, PRICE_CLOSE);
   hAtr    = iATR(_Symbol, PERIOD_CURRENT, AtrPeriod);
   if(hFastMa == INVALID_HANDLE || hSlowMa == INVALID_HANDLE || hRsi == INVALID_HANDLE || hAtr == INVALID_HANDLE)
   { Print("Indicator handles failed"); return INIT_FAILED; }

   SetupChat();
   EventSetTimer(2);
   Print("CriticalConsensusStandalone initialized. Mode: ",
         WorkMode == MODE_AUTONOMOUS ? "AUTONOMOUS (demo-only, capped)" : "CONFIRMATION (Telegram approval)");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   if(hFastMa != INVALID_HANDLE) IndicatorRelease(hFastMa);
   if(hSlowMa != INVALID_HANDLE) IndicatorRelease(hSlowMa);
   if(hRsi != INVALID_HANDLE) IndicatorRelease(hRsi);
   if(hAtr != INVALID_HANDLE) IndicatorRelease(hAtr);
}

void OnTick()
{
   datetime bar_time = iTime(_Symbol, PERIOD_CURRENT, 0);
   if(bar_time == last_bar_time) return;   // evaluate once per bar
   last_bar_time = bar_time;

   if(pending_active) return;              // one pending confirmation at a time
   if(PositionsTotal() > 0) return;        // one position at a time

   double fast[3], slow[3], rsi[3], atr[3];
   if(CopyBuffer(hFastMa, 0, 0, 3, fast) < 3) return;
   if(CopyBuffer(hSlowMa, 0, 0, 3, slow) < 3) return;
   if(CopyBuffer(hRsi, 0, 0, 3, rsi) < 3) return;
   if(CopyBuffer(hAtr, 0, 0, 3, atr) < 3) return;

   double atr_sum = 0.0;
   for(int i = 1; i <= 2; i++) atr_sum += atr[i];
   HandleSignal(fast, slow, rsi, atr, atr_sum / 2.0);
}

void OnTimer()
{
   if(!chat_known && StringLen(TelegramToken) > 0)
      SetupChat();                          // waiting for the operator's /start
   if(pending_active)
   {
      if(TimeCurrent() > pending_expires)
      {
         pending_active = false;
         TelegramSend("⌛ Approval expired — signal discarded.");
         Print("Pending confirmation expired; signal discarded (fail closed).");
         return;
      }
      PollApproval();
   }
}
