<?php
/**
 * Hardened upload receiver. Install in the web folder that UPLOAD_URL points to, e.g. /admin/data/
 *  - token read from a file OUTSIDE the web root (never hardcoded in a served file)
 *  - only known filenames are accepted; daily reports are stored under an unguessable name in reports/
 *  - reports are NOT copied into backup/ (backup/ is for the DB and dashboard XML only)
 * Deploy together with server/.htaccess (blocks direct downloads of *.db and directory listings).
 */
$token_file = "/etc/portfolio-upload/token";   // root:www-data 0640, outside the web root
$secret_token = is_readable($token_file) ? trim(file_get_contents($token_file)) : "";
if ($secret_token === "" || !isset($_POST['token']) || !hash_equals($secret_token, $_POST['token'])) {
    http_response_code(403);
    die("Unauthorized");
}
header("Content-Type: application/json");
$allowed = ["portfolio.db", "volume_dashboard.xml"];
$out = [];
foreach ($_FILES as $key => $file) {
    if ($file["error"] !== UPLOAD_ERR_OK) { $out[$key] = ["status" => "error", "code" => $file["error"]]; continue; }
    $name = basename($file["name"]);
    if ($key === "report" && preg_match('/^market-report-\d{4}-\d{2}-\d{2}-\d{4}Z\.txt$/', $name)) {
        $dir = __DIR__ . "/reports/";
        if (!is_dir($dir)) { mkdir($dir, 0750, true); file_put_contents($dir . "index.html", ""); }
        $stored = substr($name, 0, -4) . "-" . bin2hex(random_bytes(12)) . ".txt";
        if (move_uploaded_file($file["tmp_name"], $dir . $stored)) {
            $out[$key] = ["status" => "ok", "url" => "https://" . $_SERVER["HTTP_HOST"] . dirname($_SERVER["SCRIPT_NAME"]) . "/reports/" . $stored];
        } else { $out[$key] = ["status" => "error"]; }
        continue;
    }
    if (!in_array($name, $allowed, true)) { $out[$key] = ["status" => "rejected", "name" => $name]; continue; }
    if (move_uploaded_file($file["tmp_name"], __DIR__ . "/" . $name)) {
        $backup_dir = __DIR__ . "/backup/";
        if (!is_dir($backup_dir)) { mkdir($backup_dir, 0750, true); }
        $p = pathinfo($name);
        copy(__DIR__ . "/" . $name, $backup_dir . $p['filename'] . "_" . date('Y-m-d_H-i-s') . "." . $p['extension']);
        $out[$key] = ["status" => "ok", "name" => $name];
    } else { $out[$key] = ["status" => "error", "name" => $name]; }
}
echo json_encode($out);
