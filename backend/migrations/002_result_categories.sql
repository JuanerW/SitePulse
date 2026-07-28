-- Convert legacy success/failed values to the richer product categories.
UPDATE check_results
SET status = CASE
    WHEN status = 'success' THEN 'healthy'
    WHEN status_code IN (401, 403) THEN 'blocked'
    WHEN status_code = 404 THEN 'not_found'
    WHEN status_code = 429 THEN 'rate_limited'
    WHEN status_code BETWEEN 500 AND 599 THEN 'server_error'
    WHEN error = 'Request timed out' THEN 'timeout'
    WHEN error LIKE 'Network error:%' THEN 'network_error'
    ELSE 'client_error'
END
WHERE status IN ('success', 'failed');
