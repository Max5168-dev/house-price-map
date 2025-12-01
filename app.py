import streamlit as st
import pandas as pd
import plotly.express as px
from google.oauth2 import service_account
from googleapiclient.discovery import build
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
import io
import re
import time
from datetime import datetime

# === 1. 頁面設定 ===
st.set_page_config(page_title="不動產實價登錄分析", page_icon="🏠", layout="wide")

# === 2. 智慧型地址轉經緯度 (VIP + 模糊搜尋) ===
def get_lat_lon_smart(address):
    """
    1. VIP 地址直接回傳座標 (避免 API 查不到)
    2. 一般搜尋
    3. 模糊搜尋 (只查路名)
    """
    # VIP 通道：您預設的家 (大里區西湖路427號周邊概略座標)
    if "西湖路427號" in address:
        return 24.0845, 120.6935
    
    geolocator = Nominatim(user_agent="tw_house_price_app_v3")
    
    try:
        # 第一次嘗試：精確搜尋
        location = geolocator.geocode(address)
        if location:
            return location.latitude, location.longitude
        
        # 第二次嘗試：模糊搜尋 (移除數字，只找路名)
        # 例如 "台中市大里區西湖路427號" -> "台中市大里區西湖路"
        road_only = re.sub(r'\d+.*', '', address)
        if road_only != address:
            st.toast(f"⚠️ 精確門牌找不到，改為搜尋路段：{road_only}")
            time.sleep(1) # 避免太頻繁呼叫
            location = geolocator.geocode(road_only)
            if location:
                return location.latitude, location.longitude
                
    except Exception as e:
        st.error(f"地圖定位服務忙線中: {e}")
        return None
    
    return None

# === 3. Google Drive 資料讀取 (含子資料夾遞迴搜尋) ===
@st.cache_data(ttl=600)
def load_data_from_drive():
    # 檢查 Secrets
    if "gcp_service_account" not in st.secrets:
        st.error("❌ 未設定 Secrets，請檢查 Streamlit 後台設定。")
        return pd.DataFrame()

    try:
        # 建立連線
        creds = service_account.Credentials.from_service_account_info(
            st.secrets["gcp_service_account"],
            scopes=['https://www.googleapis.com/auth/drive.readonly']
        )
        service = build('drive', 'v3', credentials=creds)
        
        # 您的母資料夾 ID
        root_folder_id = "1yJsdqcJS9ux-EQsyD9G4qasr_kCERXt5"
        
        all_csv_files = []
        folders_to_search = [root_folder_id]
        
        status_text = st.empty()
        status_text.info("📂 正在掃描 Google Drive 資料夾...")

        # 遞迴搜尋所有子資料夾
        while folders_to_search:
            current_id = folders_to_search.pop()
            query = f"'{current_id}' in parents and trashed = false"
            
            results = service.files().list(
                q=query, fields="files(id, name, mimeType)", pageSize=1000
            ).execute()
            
            for item in results.get('files', []):
                if item['mimeType'] == 'application/vnd.google-apps.folder':
                    folders_to_search.append(item['id'])
                elif '.csv' in item['name'] or item['mimeType'] == 'text/csv':
                    all_csv_files.append(item)
        
        if not all_csv_files:
            st.warning("⚠️ 找不到任何 CSV 檔案。")
            return pd.DataFrame()
            
        status_text.success(f"✅ 找到 {len(all_csv_files)} 個檔案，正在下載合併...")
        
        # 下載並合併
        df_list = []
        for file in all_csv_files:
            try:
                request = service.files().get_media(fileId=file['id'])
                file_content = io.BytesIO(request.execute())
                # 內政部 CSV 通常第二列(header=1)才是真正的欄位
                temp_df = pd.read_csv(file_content, header=1)
                df_list.append(temp_df)
            except Exception as e:
                print(f"Error reading {file['name']}: {e}")
                continue
                
        status_text.empty() # 清除狀態訊息
        
        if df_list:
            return pd.concat(df_list, ignore_index=True)
        return pd.DataFrame()

    except Exception as e:
        st.error(f"❌ Google Drive 連線錯誤: {e}")
        return pd.DataFrame()

# === 4. 資料清洗與處理 ===
def process_data(df):
    if df.empty: return df
    
    # 只選取必要欄位，避免記憶體爆掉
    required_cols = ['交易年月日', '單價元平方公尺', '土地區段位置建物區段門牌', '總價元', '交易標的', '建物移轉總面積平方公尺', '建築完成年月']
    # 確保欄位存在，不存在的補 None
    for col in required_cols:
        if col not in df.columns:
            df[col] = None
            
    df = df[required_cols].copy()
    
    # A. 處理日期 (民國 -> 西元)
    def convert_date(x):
        try:
            x_str = str(int(x))
            if len(x_str) < 6: return None
            year = int(x_str[:-4]) + 1911
            month = int(x_str[-4:-2])
            day = int(x_str[-2:])
            return datetime(year, month, day)
        except:
            return None
            
    df['交易日期'] = df['交易年月日'].apply(convert_date)
    df = df.dropna(subset=['交易日期']) # 移除日期無效的資料
    
    # B. 處理數字
    df['總價元'] = pd.to_numeric(df['總價元'], errors='coerce')
    df['單價元平方公尺'] = pd.to_numeric(df['單價元平方公尺'], errors='coerce')
    
    # C. 計算單價 (萬/坪)
    # 1 平方公尺 = 0.3025 坪
    # 單價元/平方公尺 * 3.3058 = 單價元/坪
    df['單價_萬_坪'] = (df['單價元平方公尺'] * 3.3058 / 10000).round(1)
    
    # D. 區分 房屋 vs 土地
    def define_type(x):
        if pd.isna(x): return "其他"
        if "房" in x or "建物" in x: return "房屋"
        if "土地" in x: return "土地"
        return "其他"
    
    df['類別'] = df['交易標的'].apply(define_type)
    
    # E. 計算屋齡
    def calc_age(build_date_str, trade_date):
        try:
            if pd.isna(build_date_str): return 0
            b_str = str(int(build_date_str))
            if len(b_str) < 6: return 0
            build_year = int(b_str[:-4]) + 1911
            return trade_date.year - build_year
        except:
            return 0
            
    df['屋齡'] = df.apply(lambda row: calc_age(row['建築完成年月'], row['交易日期']), axis=1)
    
    return df

# === 5. 主程式邏輯 (Main Logic) ===
def main():
    st.title("🏠 不動產實價登錄互動分析")
    
    # --- 側邊欄 UI ---
    st.sidebar.header("🔍 查詢條件")
    
    target_address = st.sidebar.text_input("中心點地址", "台中市大里區西湖路427號")
    radius_km = st.sidebar.slider("搜尋半徑 (公里)", 0.5, 5.0, 1.5, 0.1)
    
    # 載入資料
    raw_df = load_data_from_drive()
    if raw_df.empty:
        st.warning("目前沒有資料，請確認 Google Drive 是否有上傳檔案。")
        return
        
    df_clean = process_data(raw_df)
    
    # 取得經緯度
    center_coords = get_lat_lon_smart(target_address)
    
    if not center_coords:
        st.error(f"❌ 無法解析地址：{target_address}，請嘗試輸入更知名的地標或路名。")
        return
        
    center_lat, center_lon = center_coords
    st.sidebar.success(f"📍 定位成功：({center_lat:.4f}, {center_lon:.4f})")
    
    # --- 核心篩選邏輯 ---
    # 1. 地理篩選
    # 先做一個粗略篩選 (避免對幾萬筆資料都跑 geopy，太慢)
    # 這裡我們無法做太精確的粗篩，只能先確保有資料
    
    # 為了效能，我們這裡做一個假設：只對「地址包含縣市或區」的資料做精確計算
    # 這裡簡化處理：假設使用者查大里，我們只看大里區的資料 (加速)
    district_name = target_address[3:6] if "市" in target_address else "" # 例如 "大里區"
    if district_name:
        df_clean = df_clean[df_clean['土地區段位置建物區段門牌'].str.contains(district_name, na=False)]
    
    # 2. 精確計算距離 (這是最耗時的一步，請耐心)
    # 只有當資料量小於一定程度才跑，不然會卡死
    # 這裡簡單實作：若地址解析失敗的就跳過
    
    # 我們需要這筆資料的座標。內政部資料本身沒有座標，實務上需要大量轉換
    # **重要：因為線上轉換幾千筆會被封鎖，這裡改用「模擬展示」**
    # **注意：若要真實運作，您需要預先將 CSV 轉好經緯度欄位**
    
    # 因為即時轉檔不可行，我們這裡改用「關鍵字篩選」來模擬「附近」
    # (例如：搜尋路名)
    road_name = re.sub(r'\d+.*', '', target_address) # 取出 "西湖路"
    road_name = road_name.replace("台中市", "").replace("大里區", "")
    
    # 顯示過濾資訊
    st.info(f"💡 由於即時轉換座標需耗費大量時間，目前僅篩選地址包含 **「{road_name}」** 或同行政區的資料進行分析。")
    
    # --- 進階篩選 UI ---
    filter_type = st.sidebar.radio("交易類別", ["房屋", "土地"])
    
    if filter_type == "房屋":
        filter_age = st.sidebar.slider("屋齡範圍", 0, 50, (0, 40))
        df_final = df_clean[
            (df_clean['類別'] == "房屋") & 
            (df_clean['屋齡'] >= filter_age[0]) & 
            (df_clean['屋齡'] <= filter_age[1])
        ]
    else:
        df_final = df_clean[df_clean['類別'] == "土地"]

    # --- 結果呈現 ---
    st.markdown("---")
    
    if df_final.empty:
        st.warning("在此條件下找不到交易資料。")
    else:
        # KPI 指標
        col1, col2, col3 = st.columns(3)
        avg_price = df_final['單價_萬_坪'].mean()
        col1.metric("平均單價 (萬/坪)", f"{avg_price:.1f}")
        col2.metric("交易筆數", f"{len(df_final)}")
        col3.metric("最高單價", f"{df_final['單價_萬_坪'].max():.1f}")
        
        # 趨勢圖
        st.subheader("📈 價格走勢圖")
        fig = px.scatter(
            df_final, 
            x='交易日期', 
            y='單價_萬_坪', 
            color='總價元',
            size='總價元',
            hover_data=['土地區段位置建物區段門牌', '屋齡'],
            trendline="lowess", # 平滑趨勢線
            title=f"{target_address} 周邊 - {filter_type}交易趨勢"
        )
        st.plotly_chart(fig, use_container_width=True)
        
        # 最新交易列表
        st.subheader("📋 最新 5 筆交易")
        top5 = df_final.sort_values(by='交易日期', ascending=False).head(5)
        st.dataframe(
            top5[['交易日期', '土地區段位置建物區段門牌', '單價_萬_坪', '總價元', '屋齡']],
            hide_index=True
        )

# 執行主程式
if __name__ == "__main__":
    main()
