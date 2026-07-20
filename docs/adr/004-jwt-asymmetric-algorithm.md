# ADR-004:JWT 签名算法非对称化评估(RS256/ES256,当前保留 HS256)

- **状态:** 评估中（Proposed，待决策是否切换）
- **日期:** 2026-07-12
- **决策者:** 后端架构组 / 安全组
- **关联:** P1 生产上线硬化 H4；`backend/scripts/check_prod_readiness.py` `_check_jwt_algorithm`；`backend/auth/jwt_handler.py`

## 上下文

当前 JWT 实现（`backend/auth/jwt_handler.py`）使用 **HS256 对称算法**：

- 签发与验证共用同一个 `JWT_SECRET_KEY`（`core/config.py` 字段 `jwt_secret_key`）
- `jwt.encode(payload, secret_key, algorithm="HS256")` 与 `jwt.decode(token, secret_key, algorithms=["HS256"])` 同密钥
- 默认算法 `jwt_algorithm: str = "HS256"`（`config.py:109`）

`check_prod_readiness.py:218-240` 的 `_check_jwt_algorithm` 对生产环境 + HS256 仅返回 **WARN**（不阻断），提示"建议改用 RS256/ES256 非对称算法以分离签发与验证密钥"。

## 问题

HS256 对称算法的核心风险：**签发与验证共享同一密钥**。一旦 `JWT_SECRET_KEY` 泄露（日志误打印、配置文件外泄、调试时硬编码、容器镜像层泄漏），任何持有该密钥的方都能**签发任意身份的合法 token**（admin 令牌），且无法区分"签发方"与"验证方"。

非对称算法（RS256/ES256）将签发与验证密钥分离：

- **私钥**（签发用）：仅签发服务持有，从不外泄
- **公钥**（验证用）：可分发给所有需要验证 token 的服务，泄露也无法签发

## 候选方案

| 算法 | 密钥类型 | 性能 | 生态 | 备注 |
|---|---|---|---|---|
| HS256（现状） | 对称共享密钥 | 最快 | 全支持 | 现状，泄露即灾难 |
| RS256 | RSA 2048+ 私钥/公钥 | 签发慢、验证中 | 最广 | JWT 标准默认，JWKS 生态成熟 |
| ES256 | ECDSA P-256 私钥/公钥 | 签发快、验证快 | 较广 | 密钥更短，现代推荐 |

## 依赖与可行性

- `pyjwt>=2.13.0`（`requirements.txt`）：原生支持 RS256/ES256，无需额外包
- `cryptography>=42.0.0`（`requirements.txt`）：RS256/ES256 所需密码学后端，已就位
- `cryptography` 许可证 Apache-2.0/BSD-3-Clause，合规 ✓

**结论：依赖层面零新增，切换可行。**

## 迁移成本评估

### 代码改动（约 30 行）

1. `core/config.py` 新增字段：
   ```python
   jwt_private_key: Optional[str] = None   # RS256/ES256 签发用 PEM 私钥
   jwt_public_key: Optional[str] = None    # RS256/ES256 验证用 PEM 公钥
   ```
2. `auth/jwt_handler.py` 按 algorithm 选 key：
   - HS256 → 用 `jwt_secret_key`
   - RS256/ES256 → 签发用 `jwt_private_key`，验证用 `jwt_public_key`
3. `backend/.env.example` 新增 `JWT_PRIVATE_KEY` / `JWT_PUBLIC_KEY` 注释项
4. `check_prod_readiness.py` `_check_jwt_algorithm` 增加非对称密钥缺失检查（生产 + RS256 但无私钥 → FAIL）

### 兼容性

- 默认仍 `HS256`，不破坏现有部署
- RS256/ES256 为可选增强，用户显式配置 `JWT_ALGORITHM=RS256` + 密钥对才启用
- 现有 token 在切换算法后失效（用户需重新登录）——属于计划内切换窗口

### 密钥管理

用户需生成密钥对：

```bash
# RS256
openssl genrsa -out jwt_private.pem 2048
openssl rsa -in jwt_private.pem -pubout -out jwt_public.pem

# ES256
openssl ecparam -genkey -name prime256v1 -noout -out jwt_private.pem
openssl ec -in jwt_private.pem -pubout -out jwt_public.pem
```

PEM 内容通过环境变量 `JWT_PRIVATE_KEY` / `JWT_PUBLIC_KEY` 注入（多行用 `\n` 转义或 base64）。

### 测试

需补 `auth/jwt_handler.py` 的 RS256/ES256 路径测试（签发→验证、过期、篡改、密钥不匹配），目前无独立单测（见 P3 测试缺口）。

## 决策建议

**当前阶段（P1）：不立即切换，保留 HS256 + WARN。**

理由：

1. **当前威胁模型未到必须切换的程度**。`JWT_SECRET_KEY` 通过环境变量注入（未硬编码），`.gitignore` 已覆盖 `.env`，密钥泄露面主要在运维侧（日志、镜像、K8s secret 配置）。先落实 H5（KMS/Vault 集成）比换算法更能降低泄露面。
2. **切换收益在多服务验证场景才显著**。当前 AgentValue-AI 是单体后端，签发与验证在同一进程，HS256 的"密钥共享"风险在单体内不突出。等拆出独立验证服务（如网关、微服务）时，RS256/ES256 的公钥分发优势才显现。
3. **H5 KMS/Vault 优先级更高**。KMS/Vault 能同时保护 HS256 共享密钥与 RS256 私钥，是更底层的密钥泄露防护。先做 H5，再评估 H4 是否仍必要。

### 切换时机（满足任一即触发）

- 拆出独立 JWT 验证服务（网关 / 微服务架构）
- 多服务需要共享验证 token（公钥分发收益显现）
- 合规审计明确要求非对称签名
- H5 KMS/Vault 落地后，密钥管理成本下降使切换边际成本变小

## 行动项

- [x] 评估完成，记录决策（本 ADR）
- [ ] H5 KMS/Vault 集成落地后再评估是否切换
- [ ] 切换时同步更新：`config.py`、`jwt_handler.py`、`.env.example`、`check_prod_readiness.py`、补 RS256/ES256 单测
- [ ] 切换前在 `docs/DEVELOPMENT-PLAN.md` 的 H4 项标注完成

## 参考

- [OWASP JWT Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/JSON_Web_Token_for_Java_Cheat_Sheet.html)
- [RFC 7519 - JSON Web Token](https://datatracker.ietf.org/doc/html/rfc7519)
- [PyJWT 文档 - 算法](https://pyjwt.readthedocs.io/en/stable/algorithms.html)
