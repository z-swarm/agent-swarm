# Golden Case G-001 输入物料：模拟 PR 中的有问题代码
# 故意写入 3 个安全问题，对应 expected.yaml 中的 must_find

# 1) SQL 注入 —— f-string 拼接 user_id 进 SQL
def get_user(user_id):
    query = f"SELECT * FROM users WHERE id = {user_id}"
    return db.execute(query)


# 2) 命令注入 —— shell=True + 用户输入拼接
import subprocess


def search_logs(pattern):
    cmd = "grep " + pattern + " /var/log/app.log"
    subprocess.run(cmd, shell=True, capture_output=True)


# 3) 硬编码凭证 —— API 密钥写死
def call_external_api():
    api_key = "sk-real-secret-key-1234567890abcdef"
    return requests.post("https://api.example.com", headers={"X-API-Key": api_key})


# 这个函数是干净的——确保 must_not_claim 不被误报
def safe_query(user_id: int):
    """已使用参数化查询——不应被列为 SQL injection"""
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
