#property strict
#property script_show_inputs

// Read-only export of the exact D1 history used by the pre-registered study.
// This script never creates, changes, or closes orders.

#define SYMBOL_COUNT 5

string Symbols[SYMBOL_COUNT] = {"XAUUSD", "XAGUSD", "EURUSD", "AUDUSD", "NZDUSD"};
int ExpectedRows[SYMBOL_COUNT] = {5753, 4633, 5932, 5933, 5931};

datetime StartDate = D'2004.01.01 00:00:00';
datetime EndDate   = D'2026.11.02 00:00:00';

string CsvFile    = "daily_basket.csv";
string StatusFile = "daily_basket.status";

void WriteFailure(const string reason)
{
   int handle = FileOpen(StatusFile, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(handle != INVALID_HANDLE)
   {
      FileWriteString(handle, "FAIL\r\n");
      FileWriteString(handle, reason + "\r\n");
      FileClose(handle);
   }
   Print("DAILY_BASKET_EXPORT_FAIL: ", reason);
}

void OnStart()
{
   FileDelete(StatusFile);
   FileDelete(CsvFile);

   if(!TerminalInfoInteger(TERMINAL_CONNECTED))
   {
      WriteFailure("TERMINAL_NOT_CONNECTED");
      return;
   }

   if(AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO)
   {
      WriteFailure("ACCOUNT_IS_NOT_DEMO");
      return;
   }

   int csv = FileOpen(CsvFile, FILE_WRITE | FILE_CSV | FILE_ANSI, ';');
   if(csv == INVALID_HANDLE)
   {
      WriteFailure("CSV_OPEN_FAILED_" + IntegerToString(GetLastError()));
      return;
   }

   FileWrite(csv, "symbol", "time", "open", "high", "low", "close", "tick_volume", "spread");

   int totalRows = 0;

   for(int s = 0; s < SYMBOL_COUNT; s++)
   {
      string symbol = Symbols[s];

      if(!SymbolSelect(symbol, true))
      {
         int errorCode = GetLastError();
         FileClose(csv);
         WriteFailure(symbol + "_SYMBOL_SELECT_FAILED_" + IntegerToString(errorCode));
         return;
      }

      MqlRates rates[];
      ArraySetAsSeries(rates, false);

      int copied = -1;
      for(int attempt = 0; attempt < 180; attempt++)
      {
         ResetLastError();
         copied = CopyRates(symbol, PERIOD_D1, StartDate, EndDate, rates);
         if(copied >= ExpectedRows[s])
            break;
         Sleep(1000);
      }

      if(copied != ExpectedRows[s])
      {
         int errorCode = GetLastError();
         FileClose(csv);
         WriteFailure(symbol + "_EXPECTED_" + IntegerToString(ExpectedRows[s]) +
                      "_BARS_GOT_" + IntegerToString(copied) +
                      "_ERROR_" + IntegerToString(errorCode));
         return;
      }

      if(copied <= 0 || ArraySize(rates) != copied)
      {
         FileClose(csv);
         WriteFailure(symbol + "_INVALID_RATE_ARRAY");
         return;
      }

      for(int i = 0; i < copied; i++)
      {
         if(i > 0 && rates[i].time <= rates[i - 1].time)
         {
            FileClose(csv);
            WriteFailure(symbol + "_NON_MONOTONIC_BAR_TIMES");
            return;
         }

         if(rates[i].open <= 0 || rates[i].high <= 0 ||
            rates[i].low <= 0 || rates[i].close <= 0 ||
            rates[i].high < rates[i].low ||
            rates[i].tick_volume < 0 || rates[i].spread < 0)
         {
            FileClose(csv);
            WriteFailure(symbol + "_INVALID_OHLCV_BAR");
            return;
         }

         FileWrite(csv,
                   symbol,
                   IntegerToString((long)rates[i].time),
                   DoubleToString(rates[i].open, 16),
                   DoubleToString(rates[i].high, 16),
                   DoubleToString(rates[i].low, 16),
                   DoubleToString(rates[i].close, 16),
                   IntegerToString(rates[i].tick_volume),
                   IntegerToString(rates[i].spread));
      }

      totalRows += copied;
      Print("DAILY_BASKET_SYMBOL_OK: ", symbol, " bars=", copied);
   }

   FileFlush(csv);
   FileClose(csv);

   int status = FileOpen(StatusFile, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(status == INVALID_HANDLE)
   {
      WriteFailure("STATUS_FILE_OPEN_FAILED_" + IntegerToString(GetLastError()));
      return;
   }

   FileWriteString(status, "PASS\r\n");
   FileWriteString(status, "DEMO\r\n");
   FileWriteString(status, IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + "\r\n");
   FileWriteString(status, AccountInfoString(ACCOUNT_SERVER) + "\r\n");
   FileWriteString(status, IntegerToString(SYMBOL_COUNT) + "\r\n");
   FileWriteString(status, IntegerToString(totalRows) + "\r\n");
   FileClose(status);

   Print("DAILY_BASKET_EXPORT_PASS: read-only; symbols=", SYMBOL_COUNT,
         " total_bars=", totalRows, " orders_submitted=0");
}
