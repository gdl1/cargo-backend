param(
    [string]$BaseUrl = 'http://127.0.0.1:8000'
)

$ErrorActionPreference = 'Stop'
$BaseUrl = $BaseUrl.TrimEnd('/')

function Invoke-CargoApi {
    param([string]$Method, [string]$Path, [object]$Body = $null)
    $request = @{
        Method = $Method
        Uri = "$BaseUrl/api/v1$Path"
        ErrorAction = 'Stop'
    }
    if ($Method -ne 'GET') {
        $request.Headers = @{ 'Idempotency-Key' = [guid]::NewGuid().ToString() }
        $request.ContentType = 'application/json; charset=utf-8'
        $json = ConvertTo-Json -InputObject $Body -Depth 30
        $request.Body = [System.Text.Encoding]::UTF8.GetBytes($json)
    }
    $response = Invoke-RestMethod @request
    if ($response.code -ne 'SUCCESS') {
        throw "요청 실패: $($response.code) / $($response.message)"
    }
    return $response.data
}

function Get-FirstArrangement {
    param([object]$Calculation)
    if (-not $Calculation.is_success -or @($Calculation.arrangements).Count -eq 0) {
        throw "배치를 선택할 수 없습니다: $($Calculation.result_code)"
    }
    return $Calculation.arrangements[0]
}

# 실행할 때마다 별도의 확인용 계획 두 개가 저장됩니다.
$null = Invoke-CargoApi 'GET' '/health/db'
$planBody = @{
    name = "연동 확인 $(Get-Date -Format 'yyyyMMdd-HHmmss')"
    category = 'LEISURE'
    memo = '기본 배치 및 짐 추가 확인'
    is_favorite = $false
    vehicle_id = 'avante_demo'
    passenger_count = 2
    passenger_seats = @('ROW_1_LEFT', 'ROW_1_RIGHT')
    car_seats = @()
    retained_seats = @()
    items = @(@{
        name = '보관 상자'
        width_cm = 20
        depth_cm = 20
        height_cm = 20
        quantity = 2
        required_quantity = 1
        allowed_orientations = @('WDH')
        dimension_source = 'MANUAL'
        transport_state = 'ASSEMBLED'
        material = '플라스틱'
    })
}
$plan = Invoke-CargoApi 'POST' '/plans' $planBody
$planId = $plan.plan_id

$calculation = Invoke-CargoApi 'POST' "/plans/$planId/calculations" @{
    expected_revision = $plan.revision
    packing_mode = 'REPACK_ALL'
    cargo_state = 'EMPTY'
    occupied_seats = @('ROW_1_LEFT', 'ROW_1_RIGHT')
    current_folded_segments = @()
    allow_exclusions = $false
}
$arrangement = Get-FirstArrangement $calculation
$null = Invoke-CargoApi 'PUT' "/plans/$planId/selection" @{
    expected_revision = $plan.revision
    calculation_id = $calculation.calculation_id
    arrangement_id = $arrangement.arrangement_id
}

$detail = Invoke-CargoApi 'GET' "/plans/$planId"
$null = Invoke-CargoApi 'GET' '/plans?limit=20&offset=0'
$null = Invoke-CargoApi 'GET' "/plans/$planId/calculations/$($calculation.calculation_id)"
if ($detail.selected_arrangement_id -ne $arrangement.arrangement_id) {
    throw '선택한 배치가 상세 조회에 반영되지 않았습니다.'
}

$previewPlan = @{
    name = "$($detail.name) - 짐 추가"
    category = $detail.category
    memo = $detail.memo
    is_favorite = $false
    vehicle_id = $detail.vehicle_id
    passenger_count = $detail.passenger_count
    passenger_seats = @($detail.passenger_seats)
    car_seats = @($detail.car_seats)
    retained_seats = @($detail.retained_seats)
    items = @($detail.items) + @(@{
        name = '추가 상자'
        width_cm = 10
        depth_cm = 10
        height_cm = 10
        quantity = 1
        required_quantity = 1
        allowed_orientations = @('WDH')
        dimension_source = 'MANUAL'
        transport_state = 'ASSEMBLED'
        material = '플라스틱'
    })
}
$preview = Invoke-CargoApi 'POST' '/calculations/preview' @{
    source_plan_id = $planId
    expected_revision = $detail.revision
    plan = $previewPlan
    calculation_options = @{
        expected_revision = $detail.revision
        packing_mode = 'KEEP_EXISTING'
        cargo_state = 'SELECTED_ARRANGEMENT'
        base_calculation_id = $detail.selected_calculation_id
        base_arrangement_id = $detail.selected_arrangement_id
        occupied_seats = @('ROW_1_LEFT', 'ROW_1_RIGHT')
        current_folded_segments = @($arrangement.target_folded_segments)
        allow_exclusions = $false
    }
}
$previewArrangement = Get-FirstArrangement $preview.calculation
$newPlan = Invoke-CargoApi 'POST' '/plans' @{
    source_preview_id = $preview.preview_id
    selected_arrangement_id = $previewArrangement.arrangement_id
}

$original = Invoke-CargoApi 'GET' "/plans/$planId"
$saved = Invoke-CargoApi 'GET' "/plans/$($newPlan.plan_id)"
if ($saved.plan_id -eq $planId -or @($original.items).Count -ne 1 -or @($saved.items).Count -ne 2) {
    throw '원본 유지 또는 새 계획 저장 결과를 확인해 주세요.'
}
if ($saved.selected_arrangement_id -eq $null) {
    throw '새 계획에 선택한 배치가 연결되지 않았습니다.'
}

Write-Host '계획 저장 → 계산 → 선택 → 조회 → 짐 추가 미리보기 → 새 계획 저장을 확인했습니다.'
Write-Host "원본 계획: $planId"
Write-Host "새 계획: $($newPlan.plan_id)"
Write-Host "계산 결과: $($calculation.result_code) / $($preview.calculation.result_code)"
if (@($calculation.warnings).Count -gt 0) {
    Write-Host "계산 안내: $($calculation.warnings -join ', ')"
}
