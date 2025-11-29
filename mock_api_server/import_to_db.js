const fs = require('fs');
const path = require('path');
const csv = require('csv-parser');
const mysql = require('mysql2/promise');
const moment = require('moment');

// コマンドライン引数
const args = process.argv.slice(2);
const forceMode = args.includes('--force');
const specificHouse = args.find(a => a.startsWith('--house='))?.split('=')[1];

if (args.includes('--help')) {
  console.log(`
Usage: node import_to_db.js [options]

Options:
  --force         既存データを削除して再インポート
  --house=XXXXX   特定のハウスIDのみインポート（例: --house=2025080001）
  --help          このヘルプを表示
`);
  process.exit(0);
}

// 環境変数から接続情報を取得
const dbConfig = {
  host: process.env.MCI_MYSQL_HOST || '127.0.0.1',
  port: process.env.MCI_MYSQL_PORT || 3306,
  user: process.env.MCI_MYSQL_USER,
  password: process.env.MCI_MYSQL_PASSWORD,
  database: process.env.MCI_MYSQL_DATABASE,
};

// ハウスIDをファイル名から抽出（202508_001.csv → 2025080001）
function getHouseIdFromFileName(fileName) {
  const match = fileName.match(/_(\d{3})\.csv$/);
  if (match) {
    const num = parseInt(match[1], 10);
    return `202508${num.toString().padStart(4, '0')}`;
  }
  return null;
}

// 日時文字列をUnix timestampに変換
function dateTimeToTimestamp(dateTimeStr) {
  return moment(dateTimeStr, 'YYYY/MM/DD HH:mm').unix();
}

// CSVファイルを読み込む
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

// バッチインサート
async function batchInsert(connection, houseId, rows) {
  const BATCH_SIZE = 5000;
  let inserted = 0;

  for (let i = 0; i < rows.length; i += BATCH_SIZE) {
    const batch = rows.slice(i, i + BATCH_SIZE);
    const values = batch.map(row => [
      houseId,
      dateTimeToTimestamp(row.date_time_jst),
      row.air_conditioner === '' ? null : parseFloat(row.air_conditioner),
      row.clothes_washer === '' ? null : parseFloat(row.clothes_washer),
      row.microwave === '' ? null : parseFloat(row.microwave),
      row.refrigerator === '' ? null : parseFloat(row.refrigerator),
      row.rice_cooker === '' ? null : parseFloat(row.rice_cooker),
      row.TV === '' ? null : parseFloat(row.TV),
      row.cleaner === '' ? null : parseFloat(row.cleaner),
      row.IH === '' ? null : parseFloat(row.IH),
      row.Heater === '' ? null : parseFloat(row.Heater),
    ]);

    const placeholders = values.map(() => '(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)').join(',');
    const flatValues = values.flat();

    await connection.execute(
      `INSERT INTO mock_energy_data (house_id, timestamp, air_conditioner, clothes_washer, microwave, refrigerator, rice_cooker, TV, cleaner, IH, Heater) VALUES ${placeholders}`,
      flatValues
    );

    inserted += batch.length;
    process.stdout.write(`\r  Inserted ${inserted}/${rows.length} rows`);
  }
  console.log();
}

async function main() {
  // 接続情報チェック
  if (!dbConfig.user || !dbConfig.password || !dbConfig.database) {
    console.error('Error: Database credentials not set.');
    console.error('Please set: MCI_MYSQL_USER, MCI_MYSQL_PASSWORD, MCI_MYSQL_DATABASE');
    process.exit(1);
  }

  console.log('Connecting to database...');
  const connection = await mysql.createConnection(dbConfig);
  console.log('Connected!');

  const testDataDir = path.join(__dirname, 'test_api_data');
  let files = fs.readdirSync(testDataDir).filter(f => f.endsWith('.csv'));

  // 特定ハウスIDのみ処理
  if (specificHouse) {
    files = files.filter(f => {
      const houseId = getHouseIdFromFileName(f);
      return houseId === specificHouse;
    });
    if (files.length === 0) {
      console.error(`No CSV file found for house_id: ${specificHouse}`);
      process.exit(1);
    }
  }

  console.log(`Found ${files.length} CSV files to import`);
  if (forceMode) console.log('Force mode: existing data will be overwritten');

  for (let i = 0; i < files.length; i++) {
    const file = files[i];
    const houseId = getHouseIdFromFileName(file);

    if (!houseId) {
      console.log(`Skipping ${file}: cannot extract house ID`);
      continue;
    }

    console.log(`[${i + 1}/${files.length}] Importing ${file} (house_id: ${houseId})...`);

    // 既存データをチェック
    const [existing] = await connection.execute(
      'SELECT COUNT(*) as count FROM mock_energy_data WHERE house_id = ?',
      [houseId]
    );

    if (existing[0].count > 0) {
      if (forceMode) {
        console.log(`  Deleting ${existing[0].count} existing rows...`);
        await connection.execute('DELETE FROM mock_energy_data WHERE house_id = ?', [houseId]);
      } else {
        console.log(`  Skipping: ${existing[0].count} rows already exist (use --force to overwrite)`);
        continue;
      }
    }

    // CSVを読み込んでインサート
    const csvPath = path.join(testDataDir, file);
    const rows = await readCSVFile(csvPath);
    await batchInsert(connection, houseId, rows);
  }

  await connection.end();
  console.log('\nImport completed!');
}

main().catch(err => {
  console.error('Error:', err);
  process.exit(1);
});
