param(
    [string]$MysqlContainer = "autooncall-full-mysql",
    [string]$RedisContainer = "autooncall-full-redis",
    [string]$RedpandaContainer = "autooncall-full-redpanda",
    [string]$LokiUrl = "http://127.0.0.1:13100"
)

$ErrorActionPreference = "Stop"

function Invoke-Docker {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    & docker @Args
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($Args -join ' ') failed with exit code $LASTEXITCODE"
    }
}

$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$sqlPath = Join-Path $PSScriptRoot "demo-seed.sql"

Write-Host "Seeding MySQL demo tables..."
Get-Content -Raw -Encoding UTF8 $sqlPath | docker exec -i $MysqlContainer mysql -uroot -p123456 --default-character-set=utf8mb4 autooncall
if ($LASTEXITCODE -ne 0) {
    throw "MySQL seed failed"
}

Write-Host "Seeding Redis incident keys..."
Invoke-Docker exec $RedisContainer redis-cli SET "order-service:redis_pool:connected_clients" "9827"
Invoke-Docker exec $RedisContainer redis-cli SET "order-service:redis_pool:maxclients" "10000"
Invoke-Docker exec $RedisContainer redis-cli HSET "incident:INC-REDIS-001:evidence" service "order-service" severity "P1" symptom "Redis connection timeout and 5xx spike" root_cause "Redis maxclients exhausted" confidence "0.86"
Invoke-Docker exec $RedisContainer redis-cli XADD "incident:INC-REDIS-001:timeline" "*" ts "2026-06-27T06:40:00Z" event "alert_fired" detail "5xx rate exceeded 8 percent"
Invoke-Docker exec $RedisContainer redis-cli XADD "incident:INC-REDIS-001:timeline" "*" ts "2026-06-27T06:42:12Z" event "log_signal" detail "Redis connection timeout in order-service"
Invoke-Docker exec $RedisContainer redis-cli XADD "incident:INC-REDIS-001:timeline" "*" ts "2026-06-27T06:44:30Z" event "redis_signal" detail "connected_clients 9827 of maxclients 10000"
Invoke-Docker exec $RedisContainer redis-cli SETEX "approval:INC-REDIS-001:pending" 86400 "increase maxclients requires human approval"

Write-Host "Seeding Redpanda topics and messages..."
$topics = @("order-service.events", "aiops.incidents", "deploy.events")
foreach ($topic in $topics) {
    docker exec $RedpandaContainer rpk topic create $topic 2>$null | Out-Null
}

$messages = @{
    "order-service.events" = @(
        '{"event":"order_failed","service":"order-service","reason":"redis_timeout","order_id":20001,"ts":"2026-06-27T06:43:00Z"}',
        '{"event":"order_failed","service":"order-service","reason":"redis_timeout","order_id":20002,"ts":"2026-06-27T06:44:00Z"}'
    )
    "aiops.incidents" = @(
        '{"incident_id":"INC-REDIS-001","service":"order-service","severity":"P1","root_cause":"redis_maxclients_exhausted","needs_approval":true}',
        '{"incident_id":"INC-MYSQL-001","service":"payment-service","severity":"P2","root_cause":"mysql_slow_query","needs_approval":true}'
    )
    "deploy.events" = @(
        '{"change_id":"CHG-10086","service":"order-service","version":"2026.06.27-1024","risk":"medium","rollback":"2026.06.26-1810"}'
    )
}

foreach ($topic in $messages.Keys) {
    foreach ($message in $messages[$topic]) {
        $message | docker exec -i $RedpandaContainer rpk topic produce $topic | Out-Null
    }
}

Write-Host "Seeding Loki log streams..."
$now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() * 1000000
$payload = @"
{
  "streams": [
    {
      "stream": {"service": "order-service", "level": "error", "environment": "prod", "incident_id": "INC-REDIS-001"},
      "values": [
        ["$($now - 3000000000)", "RedisConnectionTimeout: failed to borrow connection from redis-cluster-prod pool_wait_ms=1200 connected_clients=9827 maxclients=10000"],
        ["$($now - 2000000000)", "HTTP 503 /api/orders/create trace_id=trace-order-redis-001 downstream=redis error=connection_timeout"]
      ]
    },
    {
      "stream": {"service": "payment-service", "level": "warn", "environment": "prod", "incident_id": "INC-MYSQL-001"},
      "values": [
        ["$($now - 1000000000)", "SlowQuery detected sql_hash=9f3a-pay-report query_ms=4200 pool_waiting=37"]
      ]
    },
    {
      "stream": {"service": "inventory-service", "level": "error", "environment": "prod", "incident_id": "INC-K8S-001"},
      "values": [
        ["$now", "Pod CrashLoopBackOff restart_count=17 last_state=OOMKilled config_version=2026.06.27-0730"]
      ]
    }
  ]
}
"@

Invoke-RestMethod -Method Post -Uri "$LokiUrl/loki/api/v1/push" -ContentType "application/json" -Body $payload | Out-Null

Write-Host "Demo data seeded successfully."
