//+------------------------------------------------------------------+
//|  Реальный мост: советник читает команды и файл состояния,         |
//|  обменивается с Python через общую папку MQL5\Files (FILE_COMMON).|
//|  Никакого WebRequest — только файлы, поэтому не нужны галочки.    |
//+------------------------------------------------------------------+
#property copyright "DSH"
#property version   "1.00"
#property strict

input string CmdFile   = "dsh_real_cmd.json";
input string StateFile = "dsh_real_state.json";
input int    TimerMs   = 1000;

string ReadAll(const string name)
{
   int handle = FileOpen(name, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE)
      return "";
   string text = "";
   while(!FileIsEnding(handle))
      text += FileReadString(handle) + "\n";
   FileClose(handle);
   return text;
}

void WriteAll(const string name, const string text)
{
   int handle = FileOpen(name, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE)
      return;
   FileWriteString(handle, text);
   FileClose(handle);
}

string JsonStr(const string src, const string key)
{
   string needle = "\"" + key + "\":\"";
   int start = StringFind(src, needle);
   if(start < 0)
      return "";
   start += StringLen(needle);
   int end = StringFind(src, "\"", start);
   if(end < 0)
      return "";
   return StringSubstr(src, start, end - start);
}

double JsonNum(const string src, const string key)
{
   string needle = "\"" + key + "\":";
   int start = StringFind(src, needle);
   if(start < 0)
      return 0.0;
   start += StringLen(needle);
   string rest = StringSubstr(src, start);
   StringTrimLeft(rest);
   int pos = 0;
   string token = "";
   while(pos < StringLen(rest))
   {
      ushort ch = StringGetCharacter(rest, pos);
      if((ch >= '0' && ch <= '9') || ch == '.' || ch == '-')
      {
         token += ShortToString(ch);
         pos++;
      }
      else
         break;
   }
   return StringToDouble(token);
}

void SendState()
{
   string body = "{\"time\":" + IntegerToString((int)TimeLocal()) +
                 ",\"connected\":" + (TerminalInfoInteger(TERMINAL_CONNECTED) ? "true" : "false") +
                 ",\"login\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) +
                 ",\"server\":\"" + AccountInfoString(ACCOUNT_SERVER) + "\"" +
                 ",\"company\":\"" + AccountInfoString(ACCOUNT_COMPANY) + "\"" +
                 ",\"currency\":\"" + AccountInfoString(ACCOUNT_CURRENCY) + "\"" +
                 ",\"balance\":" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) +
                 ",\"equity\":" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) +
                 ",\"margin_free\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) +
                 ",\"trade_allowed\":" + (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ? "true" : "false") +
                 ",\"positions\":[";
   int total = PositionsTotal();
   bool first = true;
   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(!first)
         body += ",";
      first = false;
      body += "{\"ticket\":" + IntegerToString((long)ticket) +
              ",\"symbol\":\"" + PositionGetString(POSITION_SYMBOL) + "\"" +
              ",\"type\":" + IntegerToString((int)PositionGetInteger(POSITION_TYPE)) +
              ",\"volume\":" + DoubleToString(PositionGetDouble(POSITION_VOLUME), 2) +
              ",\"price\":" + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), 5) +
              ",\"profit\":" + DoubleToString(PositionGetDouble(POSITION_PROFIT), 2) +
              ",\"sl\":" + DoubleToString(PositionGetDouble(POSITION_SL), 5) +
              ",\"tp\":" + DoubleToString(PositionGetDouble(POSITION_TP), 5) + "}";
   }
   body += "]}";
   WriteAll(StateFile, body);
}

bool OpenTrade(const string symbol, const int side, const double lots,
               const double sl, const double tp, const string comment)
{
   MqlTradeRequest request;
   MqlTradeResult  result;
   ZeroMemory(request);
   ZeroMemory(result);
   request.action   = TRADE_ACTION_DEAL;
   request.symbol   = symbol;
   request.volume   = lots;
   request.type     = (side == 1) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   request.deviation = 50;
   request.magic    = 8272026;
   request.comment  = comment;
   request.type_filling = ORDER_FILLING_FOK;
   double price = (side == 1) ? SymbolInfoDouble(symbol, SYMBOL_BID)
                              : SymbolInfoDouble(symbol, SYMBOL_ASK);
   request.price = price;
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(sl > 0)
      request.sl = NormalizeDouble(sl, digits);
   if(tp > 0)
      request.tp = NormalizeDouble(tp, digits);
   if(!OrderSend(request, result))
   {
      Print("order failed: ", result.retcode, " ", result.comment);
      return false;
   }
   Print("order ok: ", result.retcode, " deal ", result.deal);
   return true;
}

bool ClosePosition(const ulong ticket)
{
   if(!PositionSelectByTicket(ticket))
      return false;
   MqlTradeRequest request;
   MqlTradeResult  result;
   ZeroMemory(request);
   ZeroMemory(result);
   string symbol = PositionGetString(POSITION_SYMBOL);
   long   type   = PositionGetInteger(POSITION_TYPE);
   double volume = PositionGetDouble(POSITION_VOLUME);
   request.action   = TRADE_ACTION_DEAL;
   request.position = ticket;
   request.symbol   = symbol;
   request.volume   = volume;
   request.type     = (type == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   request.deviation = 50;
   request.magic    = 8272026;
   request.comment  = "bridge close";
   request.type_filling = ORDER_FILLING_FOK;
   request.price = (type == POSITION_TYPE_BUY) ? SymbolInfoDouble(symbol, SYMBOL_BID)
                                               : SymbolInfoDouble(symbol, SYMBOL_ASK);
   if(!OrderSend(request, result))
   {
      Print("close failed: ", result.retcode, " ", result.comment);
      return false;
   }
   Print("close ok: ", result.retcode);
   return true;
}

void ProcessCommand()
{
   string raw = ReadAll(CmdFile);
   if(StringLen(raw) < 10)
      return;
   FileDelete(CmdFile, FILE_COMMON);
   string action = JsonStr(raw, "action");
   string symbol = JsonStr(raw, "symbol");
   double lots   = JsonNum(raw, "lots");
   double sl     = JsonNum(raw, "sl");
   double tp     = JsonNum(raw, "tp");
   double side   = JsonNum(raw, "side");
   double ticket = JsonNum(raw, "ticket");
   if(action == "open" && StringLen(symbol) > 0 && lots > 0)
      OpenTrade(symbol, (int)side, lots, sl, tp, "bridge open");
   else if(action == "close" && ticket > 0)
      ClosePosition((ulong)ticket);
}

int OnInit()
{
   EventSetTimer(1);
   Print("DSH real bridge started, common files: ", CmdFile, " / ", StateFile);
   SendState();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   ProcessCommand();
   SendState();
}
