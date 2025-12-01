# app.py
import io
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st
from geopy.distance import geodesic
from geopy.geocoders import Nominatim
from google.oauth2 import service_account
from googleapiclient.discovery import build

# --------------------------------------------------
# Streamlit 基本設定
# --------------------------------------------------
st.set_page_config(
    page_title="台灣不動產實價登錄互動分析系統",
    layout="wide",
)

st.title("🏠 台灣不動產實價登錄互動分析系統")

# --------------------------------------------------
# GCP / Google Drive 設定
# --------------------------------------------------
GOOGLE_DRIVE_FOLDER_ID = "1yJsdqcJS9ux-EQsyD9G4qasr_kCERXt5"


def get_gcp_credentials():
    """
    從 st.secrets["gcp_service_account"] 讀取 GCP Service Account 憑證。
    secrets.toml 中需有：
    [gcp_service_account]
    type = "service_account"
    project_id = "..."
    ...
    """
    service_account_info = st.secrets["gcp_service_account"]
    creds = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return creds


# --------------------------------------------------
# 讀取 Google Drive CSV 並快取
# --------------------------------------------------
@st.cache_data(show_spinner=True)
def load_real_price_data():
    """
    1. 連線 Google Drive，搜尋指定 Folder ID 底下所有 .csv 檔
    2. 逐檔下載後以 pd.read_csv(io.BytesIO(content), header=1) 讀取
    3. 使用 pd.concat 合併
    """
    creds = get_gcp_credentials()
    drive_service = build("drive", "v3", credentials=creds)

    query = (
        f"'{GOOGLE_DRIVE_FOLDER_ID}' in parents "
        "and mimeType='text/csv' and trashed=false"
    )

    files = []
    page_token = None
    while True:
        response = (
            drive_service.files()
            .list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name)",
                pageToken=page_token,
            )
            .execute()
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken", None)
        if page_token is None:
            break

    if not files:
        raise RuntimeError("指定的 Google Drive 資料夾內沒有找到任何 CSV 檔案。")

    dataframes = []
    for f in files:
        file_id = f["id"]
        file_name = f["name"]
        # 下載檔案內容（bytes）
        content = (
            drive_service.files().get_media(fileId=file_id).execute()
        )  # bytes
        # 內政部檔案第 0 列為說明，第 1 列才是標題
        df = pd.read_csv(io.BytesIO(content), header=1)
        df["來源檔案"] = file_name
        dataframes.append(df)

    combined_df = pd.concat(dataframes, ignore_index=True)
    return combined_df


# --------------------------------------------------
# 資料清洗與衍生欄位
# --------------------------------------------------
def parse_roc_date(value):
    """
    將「交易年月日」如 1120520 轉為 datetime (西元年)。
    若轉換失敗回傳 NaT。
    """
    if pd.isna(value):
        return pd.NaT
    s = str(value).strip()
    if len(s) not in (6, 7):
        return pd.NaT
    try:
        roc_year = int(s[:3])
        month = int(s[3:5])
        day = int(s[5:7])
        year = roc_year + 1911
        return datetime(year, month, day)
    except Exception:
        return pd.NaT


def compute_building_age(roc_ym_value, now_year=None):
    """
    根據「建築完成年月」計算屋齡 (年)。
    roc_ym_value 例如 8906 或 11205；只取前三碼為民國年。
    若為空或錯誤則回傳 0。
    """
    if now_year is None:
        now_year = datetime.now().year

    if pd.isna(roc_ym_value):
        return 0

    s = str(roc_ym_value).strip()
    if len(s) < 3:
        return 0
    try:
        roc_year = int(s[:3])
        year = roc_year + 1911
        age = now_year - year
        if age < 0:
            return 0
        return age
    except Exception:
        return 0


def categorize_property(target_str):
    """
    根據「交易標的」內容將交易類別歸類：
    - 若包含「房」或「建物」 => "房屋"
    - 若僅包含「土地」且不含建物 => "土地"
    其餘則標為 "其他"
    """
    s = str(target_str)
    if any(x in s for x in ["房", "建物"]):
        return "房屋"
    if "土地" in s:
        return "土地"
    return "其他"


def clean_real_price_data(raw_df: pd.DataFrame) -> pd.DataFrame:
    df = raw_df.copy()

    # 轉換交易日期
    if "交易年月日" in df.columns:
        df["交易日期"] = df["交易年月日"].apply(parse_roc_date)
    else:
        df["交易日期"] = pd.NaT

    # 單價萬/坪
    # 單價元平方公尺 * 3.3058 / 10000
    unit_col = "單價元平方公尺"
    if unit_col in df.columns:
        df[unit_col] = pd.to_numeric(df[unit_col], errors="coerce")
        df["單價_萬_坪"] = (df[unit_col] * 3.3058) / 10000
    else:
        df["單價_萬_坪"] = np.nan

    # 類別劃分
    if "交易標的" in df.columns:
        df["類別"] = df["交易標的"].apply(categorize_property)
    else:
        df["類別"] = "其他"

    # 屋齡計算（房屋才有意義，土地統一為 0）
    now_year = datetime.now().year
    if "建築完成年月" in df.columns:
        df["屋齡"] = df["建築完成年月"].apply(
            lambda x: compute_building_age(x, now_year=now_year)
        )
    else:
        df["屋齡"] = 0

    df.loc[df["類別"] == "土地", "屋齡"] = 0

    # 經緯度欄位標準化
    lat_col = None
    lon_col = None
    for c in df.columns:
        if c in ["緯度", "latitude", "Latitude", "LAT", "lat"]:
            lat_col = c
        if c in ["經度", "longitude", "Longitude", "LON", "lon", "lng"]:
            lon_col = c

    if lat_col is not None and lon_col is not None:
        df["lat"] = pd.to_numeric(df[lat_col], errors="coerce")
        df["lon"] = pd.to_numeric(df[lon_col], errors="coerce")
    else:
        df["lat"] = np.nan
        df["lon"] = np.nan

    # 地址欄位標準化
    addr_col = None
    for c in ["土地位置建物門牌", "location", "地址"]:
        if c in df.columns:
            addr_col = c
            break

    if addr_col:
        df["地址"] = df[addr_col].astype(str)
    else:
        df["地址"] = ""

    # 總價欄位標準化
    price_col = None
    for c in ["總價元", "總價", "price"]:
        if c in df.columns:
            price_col = c
            break

    if price_col:
        df["總價元"] = pd.to_numeric(df[price_col], errors="coerce")
    else:
        df["總價元"] = np.nan

    # 清掉沒有交易日期或單價的資料
    df = df[~df["交易日期"].isna()]
    df = df[~df["單價_萬_坪"].isna()]
    df.reset_index(drop=True, inplace=True)

    return df


# --------------------------------------------------
# Geocoding：把中心點地址轉成經緯度
# --------------------------------------------------
@st.cache_data(show_spinner=False)
def geocode_address(address: str):
    """
    使用 Nominatim 將地址轉為 (lat, lon)。
    若查詢失敗回傳 (None, None)。
    """
    geolocator = Nominatim(user_agent="tw-real-price-app", timeout=10)
    try:
        location = geolocator.geocode(address)
        if location:
            return location.latitude, location.longitude
        return None, None
    except Exception:
        return None, None


# --------------------------------------------------
# 距離計算與資料過濾
# --------------------------------------------------
def filter_by_distance_and_condition(
    df: pd.DataFrame,
    center_lat: float,
    center_lon: float,
    radius_km: float,
    transaction_type: str,
    max_house_age: int | None,
) -> pd.DataFrame:
    """
    1. 根據中心點與半徑過濾資料
    2. 根據交易類別與屋齡（若為房屋）篩選
    """
    df = df.copy()

    # 有經緯度的才計算距離
    valid_geo = df.dropna(subset=["lat", "lon"]).copy()

    def calc_distance(row):
        return geodesic(
            (center_lat, center_lon),
            (row["lat"], row["lon"]),
        ).km

    valid_geo["距離_km"] = valid_geo.apply(calc_distance, axis=1)

    # 半徑範圍內
    filtered = valid_geo[valid_geo["距離_km"] <= radius_km]

    # 類別篩選
    filtered = filtered[filtered["類別"] == transaction_type]

    # 屋齡篩選（僅房屋）
    if transaction_type == "房屋" and max_house_age is not None:
        filtered = filtered[filtered["屋齡"] <= max_house_age]

    filtered = filtered.sort_values("交易日期", ascending=False)
    return filtered


# --------------------------------------------------
# 主程式：UI + 邏輯
# --------------------------------------------------
def main():
    # Sidebar：條件設定
    with st.sidebar:
        st.header("🔎 搜尋條件")

        center_address = st.text_input(
            "中心點地址",
            value="台中市大里區西湖路427號",
            help="請輸入欲分析的中心點地址",
        )

        radius_km = st.slider(
            "搜尋半徑 (公里)",
            min_value=0.5,
            max_value=10.0,
            value=1.5,
            step=0.5,
        )

        transaction_type = st.radio(
            "交易類別",
            options=["房屋", "土地"],
            index=0,
            horizontal=True,
        )

        max_age = None
        if transaction_type == "房屋":
            max_age = st.slider(
                "屋齡上限 (年)",
                min_value=0,
                max_value=40,
                value=30,
            )

        st.markdown("---")
        st.caption("資料來源：內政部不動產交易實價登錄公開資料（Google Drive CSV）")

    # 取得中心點座標
    with st.spinner("📍 正在解析中心點地址座標..."):
        center_lat, center_lon = geocode_address(center_address)

    if center_lat is None or center_lon is None:
        st.error("無法解析此地址的經緯度，請嘗試更精確的地址或換一個地址。")
        return

    # 顯示中心點資訊
    st.markdown(
        f"**中心點座標：** {center_lat:.6f}, {center_lon:.6f}（半徑 {radius_km} km）"
    )

    # 讀取與清洗資料
    with st.spinner("📂 正在從 Google Drive 讀取實價登錄資料並進行資料清洗..."):
        try:
            raw_df = load_real_price_data()
        except Exception as e:
            st.error(f"讀取 Google Drive 資料時發生錯誤：{e}")
            return

        df = clean_real_price_data(raw_df)

    # 過濾資料
    with st.spinner("📊 正在依條件篩選資料..."):
        filtered_df = filter_by_distance_and_condition(
            df=df,
            center_lat=center_lat,
            center_lon=center_lon,
            radius_km=radius_km,
            transaction_type=transaction_type,
            max_house_age=max_age,
        )

    if filtered_df.empty:
        st.warning("在此條件下查無交易資料，請調整搜尋半徑或條件後再試。")
        return

    # --------------------------------------------------
    # Metrics：統計資訊卡片
    # --------------------------------------------------
    avg_price = filtered_df["單價_萬_坪"].mean()
    max_price = filtered_df["單價_萬_坪"].max()
    count = len(filtered_df)

    col1, col2, col3 = st.columns(3)
    col1.metric("平均單價 (萬元 / 坪)", f"{avg_price:,.2f}")
    col2.metric("搜尋範圍內交易筆數", f"{count:,d}")
    col3.metric("最高單價 (萬元 / 坪)", f"{max_price:,.2f}")

    # --------------------------------------------------
    # 最新交易列表 Top 5
    # --------------------------------------------------
    st.subheader("📝 最新交易紀錄（Top 5）")
    latest_df = filtered_df.sort_values("交易日期", ascending=False).head(5).copy()
    latest_df_display = latest_df[
        [
            "交易日期",
            "地址",
            "總價元",
            "單價_萬_坪",
            "屋齡",
            "距離_km",
        ]
    ].copy()
    latest_df_display["交易日期"] = latest_df_display["交易日期"].dt.date
    latest_df_display["總價元"] = latest_df_display["總價元"].round(0).astype("Int64")
    latest_df_display["單價_萬_坪"] = latest_df_display["單價_萬_坪"].round(2)
    latest_df_display["距離_km"] = latest_df_display["距離_km"].round(3)

    st.dataframe(
        latest_df_display,
        use_container_width=True,
        hide_index=True,
    )

    # --------------------------------------------------
    # 趨勢散佈圖：單價 vs 交易日期
    # --------------------------------------------------
    st.subheader("📈 單價趨勢散佈圖")

    scatter_df = filtered_df.copy()
    scatter_df = scatter_df.sort_values("交易日期")

    fig_scatter = px.scatter(
        scatter_df,
        x="交易日期",
        y="單價_萬_坪",
        color="總價元",
        size="總價元",
        hover_data={
            "地址": True,
            "總價元": ":,",
            "屋齡": True,
            "單價_萬_坪": ":.2f",
        },
        labels={
            "交易日期": "交易日期",
            "單價_萬_坪": "單價 (萬元 / 坪)",
            "總價元": "總價 (元)",
        },
        title="交易單價散佈圖（顏色・大小代表總價）",
        trendline="ols",  # 若環境未安裝 statsmodels 可能會失敗
    )
    fig_scatter.update_layout(
        height=500,
        xaxis_title="交易日期",
        yaxis_title="單價 (萬元 / 坪)",
    )

    st.plotly_chart(fig_scatter, use_container_width=True)

    # --------------------------------------------------
    # 地圖：交易點分佈
    # --------------------------------------------------
    st.subheader("🗺️ 交易位置分佈圖")

    map_df = filtered_df.dropna(subset=["lat", "lon"]).copy()
    if map_df.empty:
        st.info("此資料集中沒有經緯度資訊，因此無法繪製地圖。")
    else:
        fig_map = px.scatter_mapbox(
            map_df,
            lat="lat",
            lon="lon",
            color="單價_萬_坪",
            size="總價元",
            hover_name="地址",
            hover_data={
                "單價_萬_坪": ":.2f",
                "總價元": ":,",
                "屋齡": True,
                "距離_km": ":.3f",
            },
            zoom=14,
            height=550,
            title="搜尋範圍內交易分佈（顏色為單價，大小為總價）",
        )
        fig_map.update_layout(mapbox_style="open-street-map")
        fig_map.update_layout(margin=dict(l=0, r=0, t=40, b=0))

        st.plotly_chart(fig_map, use_container_width=True)


if __name__ == "__main__":
    main()
