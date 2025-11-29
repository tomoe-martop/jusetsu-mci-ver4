const express = require('express');
const fs = require('fs');
const csv = require('csv-parser');
const moment = require('moment');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 3000;

// DB接続設定（環境変数から取得）
let dbPool = null;
const useDatabase = process.env.USE_DATABASE === 'true';

if (useDatabase) {
  const mysql = require('mysql2/promise');
  const mysqlHost = process.env.MCI_MYSQL_HOST || '127.0.0.1';

  // Cloud SQLソケットパスの場合（/cloudsql/...）
  const poolConfig = {
    user: process.env.MCI_MYSQL_USER,
    password: process.env.MCI_MYSQL_PASSWORD,
    database: process.env.MCI_MYSQL_DATABASE,
    waitForConnections: true,
    connectionLimit: 10,
    queueLimit: 0
  };

  if (mysqlHost.startsWith('/cloudsql/')) {
    poolConfig.socketPath = mysqlHost;
    console.log(`Database mode enabled (Cloud SQL socket: ${mysqlHost})`);
  } else {
    poolConfig.host = mysqlHost;
    poolConfig.port = process.env.MCI_MYSQL_PORT || 3306;
    console.log(`Database mode enabled (TCP: ${mysqlHost})`);
  }

  dbPool = mysql.createPool(poolConfig);
}

// CSVデータのメモリキャッシュ（CSV使用時のみ）
const csvCache = new Map();

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

// DBからデータを取得
async function getDataFromDB(houseId, sts, ets) {
  const [rows] = await dbPool.execute(
    `SELECT timestamp, air_conditioner, clothes_washer, microwave, refrigerator,
            rice_cooker, TV, cleaner, IH, Heater
     FROM mock_energy_data
     WHERE house_id = ? AND timestamp >= ? AND timestamp < ?
     ORDER BY timestamp`,
    [houseId, sts, ets]
  );
  return rows;
}

// DBデータをAPIレスポンス形式に変換
function convertDBToAPIResponse(dbRows) {
  if (dbRows.length === 0) {
    return {
      data: [{
        timestamps: [],
        appliance_types: []
      }]
    };
  }

  const timestamps = dbRows.map(row => row.timestamp);
  const applianceTypes = [];

  Object.entries(APPLIANCE_TYPE_MAP).forEach(([columnName, applianceTypeId]) => {
    const powers = dbRows.map(row => row[columnName]);
    applianceTypes.push({
      appliance_type_id: applianceTypeId,
      appliances: [{ powers }]
    });
  });

  return {
    data: [{
      timestamps,
      appliance_types: applianceTypes
    }]
  };
}

// === CSV関連の関数（フォールバック用） ===

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

function getFileNumberFromHouseId(houseId) {
  const match = houseId.match(/^202508(\d{4})$/);
  if (match) {
    return parseInt(match[1], 10).toString().padStart(3, '0');
  }
  return null;
}

function findCSVFileByHouseId(houseId) {
  const fileNumber = getFileNumberFromHouseId(houseId);
  if (!fileNumber) return null;

  const testDataDir = path.join(__dirname, 'test_api_data');
  if (!fs.existsSync(testDataDir)) return null;

  const files = fs.readdirSync(testDataDir);
  const pattern = new RegExp(`_${fileNumber}\\.csv$`);
  const matchedFile = files.find(file => pattern.test(file));

  return matchedFile ? path.join(testDataDir, matchedFile) : null;
}

async function getCachedCSVData(filePath) {
  if (csvCache.has(filePath)) {
    return csvCache.get(filePath);
  }
  const data = await readCSVFile(filePath);
  csvCache.set(filePath, data);
  return data;
}

function dateTimeToTimestamp(dateTimeStr) {
  return moment(dateTimeStr, 'YYYY/MM/DD HH:mm').unix();
}

function convertCSVToAPIResponse(csvData, sts, ets) {
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

  const timestamps = filteredData.map(row => dateTimeToTimestamp(row.date_time_jst));
  const applianceTypes = [];

  Object.entries(APPLIANCE_TYPE_MAP).forEach(([columnName, applianceTypeId]) => {
    const powers = filteredData.map(row => {
      const value = row[columnName];
      if (!value || value === '') return null;
      return parseFloat(value);
    });

    applianceTypes.push({
      appliance_type_id: applianceTypeId,
      appliances: [{ powers }]
    });
  });

  return {
    data: [{
      timestamps,
      appliance_types: applianceTypes
    }]
  };
}

// === APIエンドポイント ===

app.get('/0.2/estimated_data', async (req, res) => {
  try {
    const { service_provider, house, sts, ets, time_units } = req.query;
    const startTime = Date.now();

    console.log(`Request: spid=${service_provider}, house=${house}, sts=${sts}, ets=${ets}, time_units=${time_units}`);

    if (!service_provider || !house || !sts || !ets) {
      return res.status(400).json({ error: 'Missing required parameters' });
    }

    if (service_provider !== '9991') {
      return res.status(404).json({ error: 'Service provider not found' });
    }

    let response;

    // DBモードの場合
    if (useDatabase && dbPool) {
      console.log('Using database...');
      const dbRows = await getDataFromDB(house, parseInt(sts), parseInt(ets));

      if (dbRows.length === 0) {
        return res.status(404).json({ error: `No data found for house: ${house}` });
      }

      response = convertDBToAPIResponse(dbRows);
    }
    // CSVフォールバック
    else {
      console.log('Using CSV files...');
      const csvFilePath = findCSVFileByHouseId(house);
      if (!csvFilePath) {
        return res.status(404).json({ error: `CSV file not found for house: ${house}` });
      }

      console.log(`Found CSV file: ${csvFilePath}`);
      const csvData = await getCachedCSVData(csvFilePath);
      response = convertCSVToAPIResponse(csvData, parseInt(sts), parseInt(ets));
    }

    const elapsed = Date.now() - startTime;
    console.log(`Returning ${response.data[0].timestamps.length} data points in ${elapsed}ms`);

    res.json(response);

  } catch (error) {
    console.error('Error:', error);
    res.status(500).json({ error: 'Internal server error', message: error.message });
  }
});

// ヘルスチェック
app.get('/health', async (req, res) => {
  const status = { status: 'ok', mode: useDatabase ? 'database' : 'csv' };

  if (useDatabase && dbPool) {
    try {
      await dbPool.execute('SELECT 1');
      status.database = 'connected';
    } catch (err) {
      status.database = 'disconnected';
      status.error = err.message;
    }
  }

  res.json(status);
});

// サーバー起動
app.listen(PORT, () => {
  console.log(`Mock API server running on http://localhost:${PORT}`);
  console.log(`Mode: ${useDatabase ? 'Database' : 'CSV files'}`);
  console.log(`Endpoint: http://localhost:${PORT}/0.2/estimated_data`);
  console.log(`Example: http://localhost:${PORT}/0.2/estimated_data?service_provider=9991&house=2025080001&sts=1718294400&ets=1718380800&time_units=20`);
});
