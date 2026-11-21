-- Q1: How many transactions per status?
SELECT status, COUNT(*) AS num_txns
FROM transactions
GROUP BY status;

-- Q2: Success rate per week (SUCCESS / (SUCCESS + FAILED))
SELECT strftime('%W', txn_ts) AS week_no,
       COUNT(*) AS num_txns,
       SUM(status = 'SUCCESS') AS num_success,
       SUM(status = 'FAILED') AS num_failed,
       ROUND(100.0 * SUM(status = 'SUCCESS')
             / SUM(status IN ('SUCCESS', 'FAILED')), 2) AS success_rate_pct
FROM transactions
GROUP BY week_no
ORDER BY week_no;

-- Q3: Mix shift - traffic and success rate per method, last two weeks
SELECT strftime('%W', txn_ts) AS week_no,
       method_id,
       COUNT(*) AS num_txns,
       ROUND(100.0 * SUM(status = 'SUCCESS')
             / SUM(status IN ('SUCCESS', 'FAILED')), 2) AS success_rate_pct
FROM transactions
WHERE strftime('%W', txn_ts) IN ('44', '45')
GROUP BY week_no, method_id
ORDER BY method_id, week_no;

-- Q4: Rate drop - Bank C (bank_id = 3) success rate per day in the last week
SELECT DATE(txn_ts) AS day,
       COUNT(*) AS num_txns,
       ROUND(100.0 * SUM(status = 'SUCCESS')
             / SUM(status IN ('SUCCESS', 'FAILED')), 2) AS success_rate_pct
FROM transactions
WHERE bank_id = 3 AND DATE(txn_ts) >= '2026-11-09'
GROUP BY day
ORDER BY day;

-- Q5: Two sources disagree - successful txns vs settled txns
SELECT (SELECT COUNT(*) FROM transactions WHERE status = 'SUCCESS') AS txn_source_success,
       (SELECT SUM(txn_count) FROM settlements) AS settlement_source_count;

-- Q6: Deduplicated success count (each txn_id counted once)
SELECT COUNT(DISTINCT txn_id) AS unique_success
FROM transactions
WHERE status = 'SUCCESS';