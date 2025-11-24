const express = require('express');
const fs = require('fs');
const csv = require('csv-parser');
const moment = require('moment');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 3000;

// 家電タイプIDのマッピング
const APPLIANCE_TYPE_MAP = {
  'air_conditioner': 2,
  'clothes_washer': 5,
  'microwave': 20,
  'refrigerator': 24,
  'rice_cooker': 25,
  'TV': 30,
  'cleaner': 31,
  'IH': 37,
  'Heater': 301
};

// CSVファイルからデータを読み込む
function readCSVFile(filePath) {
  return new Promise((resolve, reject) => {
    const results = [];
    fs.createReadStream(filePath)
      .pipe(csv())
      .on('data', (data) => results.push(data))
      .on('end', () => resolve(results))
      .on('error', (error) => reject(error));
  });
}

// ハウスIDからファイル番号を抽出（DUMMY00001 → 001）
function getFileNumberFromHouseId(houseId) {
  const match = houseId.match(/DUMMY0*(\d+)/);
  if (match) {
    // 先頭の0を除去してから3桁にパディング
    return match[1].padStart(3, '0');
  }
  return null;
}

// test_api_dataディレクトリからハウスIDに対応するCSVファイルを探す
function findCSVFileByHouseId(houseId) {
  const fileNumber = getFileNumberFromHouseId(houseId);
  if (!fileNumber) {
    return null;
  }

  const testDataDir = path.join(__dirname, 'test_api_data');

  // test_api_dataディレクトリのすべてのファイルを検索
  const files = fs.readdirSync(testDataDir);

  // _XXX.csvのパターンにマッチするファイルを探す
  const pattern = new RegExp(`_${fileNumber}\\.csv$`);
  const matchedFile = files.find(file => pattern.test(file));

  if (matchedFile) {
    return path.join(testDataDir, matchedFile);
  }

  return null;
}

// 日時文字列をUnix timestampに変換
function dateTimeToTimestamp(dateTimeStr) {
  // "2024/06/14 00:00" → Unix timestamp
  return moment(dateTimeStr, 'YYYY/MM/DD HH:mm').unix();
}

// CSVデータをAPIのJSON形式に変換
function convertCSVToAPIResponse(csvData, sts, ets) {
  // 期間でフィルタリング
  const filteredData = csvData.filter(row => {
    if (!row.date_time_jst) return false;
    const timestamp = dateTimeToTimestamp(row.date_time_jst);
    return timestamp >= sts && timestamp < ets;
  });

  if (filteredData.length === 0) {
    return {
      data: [{
        timestamps: [],
        appliance_types: []
      }]
    };
  }

  // timestampsを作成
  const timestamps = filteredData.map(row => dateTimeToTimestamp(row.date_time_jst));

  // appliance_typesを作成
  const applianceTypes = [];

  // デバッグ: 最初の行のキーを確認
  if (filteredData.length > 0) {
    console.log('First row keys:', Object.keys(filteredData[0]));
    console.log('First row sample:', {
      date_time_jst: filteredData[0].date_time_jst,
      air_conditioner: filteredData[0].air_conditioner
    });
  }

  Object.entries(APPLIANCE_TYPE_MAP).forEach(([columnName, applianceTypeId]) => {
    const powers = filteredData.map(row => {
      const value = row[columnName];
      // 空欄やnullの場合はnull、それ以外は数値に変換
      if (!value || value === '') {
        return null;
      }
      return parseFloat(value);
    });

    applianceTypes.push({
      appliance_type_id: applianceTypeId,
      appliances: [
        {
          powers: powers
        }
      ]
    });
  });

  return {
    data: [
      {
        timestamps: timestamps,
        appliance_types: applianceTypes
      }
    ]
  };
}

// APIエンドポイント
app.get('/0.2/estimated_data', async (req, res) => {
  try {
    const { service_provider, house, sts, ets, time_units } = req.query;

    console.log(`Request: spid=${service_provider}, house=${house}, sts=${sts}, ets=${ets}, time_units=${time_units}`);

    // パラメータバリデーション
    if (!service_provider || !house || !sts || !ets) {
      return res.status(400).json({ error: 'Missing required parameters' });
    }

    // spidが9991でない場合はエラー
    if (service_provider !== '9991') {
      return res.status(404).json({ error: 'Service provider not found' });
    }

    // ハウスIDに対応するCSVファイルを検索
    const csvFilePath = findCSVFileByHouseId(house);
    if (!csvFilePath) {
      return res.status(404).json({ error: `CSV file not found for house: ${house}` });
    }

    console.log(`Found CSV file: ${csvFilePath}`);

    // CSVファイルを読み込む
    const csvData = await readCSVFile(csvFilePath);

    // APIレスポンス形式に変換
    const response = convertCSVToAPIResponse(csvData, parseInt(sts), parseInt(ets));

    console.log(`Returning ${response.data[0].timestamps.length} data points`);

    res.json(response);

  } catch (error) {
    console.error('Error:', error);
    res.status(500).json({ error: 'Internal server error', message: error.message });
  }
});

// ヘルスチェック
app.get('/health', (req, res) => {
  res.json({ status: 'ok' });
});

app.listen(PORT, () => {
  console.log(`Mock API server running on http://localhost:${PORT}`);
  console.log(`Endpoint: http://localhost:${PORT}/0.2/estimated_data`);
  console.log(`Example: http://localhost:${PORT}/0.2/estimated_data?service_provider=9991&house=DUMMY00001&sts=1718294400&ets=1718380800&time_units=20`);
});