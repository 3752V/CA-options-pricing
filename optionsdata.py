from openbb import obb
from datetime import datetime, timedelta
import pandas as pd
import asyncio
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

def save_to_parquet(df: pd.DataFrame, symbol: str):
    path = Path(f"data/2024_{symbol}.parquet")
    df.to_parquet(path)


async def fetch_options_for_symbol(symbol: str, date_range: List[datetime.date], semaphore: asyncio.Semaphore):
    failed_dates = []
    dataframes = []

    async with semaphore:
        for options_day in date_range:
            try:
                options = obb.derivatives.options.chains(symbol, date=options_day, provider="tmx")
                df = options.results.dataframe
                df["query_date"] = options_day
                dataframes.append(df)
            except Exception as e:
                print(f"Failed to fetch options for {symbol} on {options_day}: {e}")
                failed_dates.append(options_day)
    
    if dataframes:
        final_df = pd.concat(dataframes, ignore_index=True)
        final_df.set_index("query_date", inplace=True)
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor() as pool:
            await loop.run_in_executor(pool, save_to_parquet, final_df, symbol)

    return symbol if failed_dates else None


async def fetch_options(symbols: List[str], start_date: datetime.date, end_date: Optional[datetime.date] = None):
    obb.user.preferences.output_type = "OBBject"
    date_range = [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)] if end_date else [start_date]
    
    semaphore = asyncio.Semaphore(5)  # Limit concurrency to avoid overwhelming the API
    tasks = [fetch_options_for_symbol(symbol, date_range, semaphore) for symbol in symbols]
    failed_symbols = await asyncio.gather(*tasks)
    return [symbol for symbol in failed_symbols if symbol]

# def save_options_data(symbols: List[str], start_date: datetime.date, end_date: Optional[datetime.date] = None):
#     failed = asyncio.run(fetch_options(symbols, start_date, end_date))
#     return failed

# if __name__ == "__main__":
#     from datetime import datetime, timedelta
#     option_to_fetch = ["RY","SHOP", "TD", "ENB", "BN", "BMO", "TRI", "BNS", "CSU", "CP"]
#     failed = asyncio.run(fetch_options(option_to_fetch, datetime.now().date()))
#     if failed:
#         print(f"Failed symbols: {set(failed)}")
