<template>
  <div class="admin-tools">
    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="mb-16"
    >
      <template #title>
        工具管理中心 —— 对标 Dify 工具市场 / Coze 插件管理。支持内置工具（calculator/datetime）、
        Toolkit 工具（employee_history/company_kb）、MCP 外部服务器工具、ReAct Agent 复杂推理调试。
      </template>
    </el-alert>

    <!-- 顶部状态卡片 -->
    <el-row :gutter="16" class="mb-16">
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-card">
            <div class="stat-icon langchain">
              <el-icon><Connection /></el-icon>
            </div>
            <div class="stat-body">
              <div class="stat-label">LangChain</div>
              <div class="stat-value">
                <el-tag :type="toolData.langchain_available ? 'success' : 'danger'" size="small">
                  {{ toolData.langchain_available ? '已安装' : '未安装' }}
                </el-tag>
              </div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-card">
            <div class="stat-icon mcp">
              <el-icon><Link /></el-icon>
            </div>
            <div class="stat-body">
              <div class="stat-label">MCP 适配器</div>
              <div class="stat-value">
                <el-tag :type="toolData.mcp_available ? 'success' : 'danger'" size="small">
                  {{ toolData.mcp_available ? '已安装' : '未安装' }}
                </el-tag>
              </div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-card">
            <div class="stat-icon builtin">
              <el-icon><Tools /></el-icon>
            </div>
            <div class="stat-body">
              <div class="stat-label">内置/Toolkit 工具</div>
              <div class="stat-value">{{ toolData.builtin?.length || 0 }} 个</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-card">
            <div class="stat-icon mcp-tools">
              <el-icon><Box /></el-icon>
            </div>
            <div class="stat-body">
              <div class="stat-label">MCP 工具</div>
              <div class="stat-value">{{ toolData.mcp?.length || 0 }} 个</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 工具列表 + 测试面板 -->
    <el-row :gutter="16">
      <el-col :span="14">
        <el-card v-loading="loading" :aria-busy="loading">
          <template #header>
            <div class="card-header-row">
              <span class="section-title">
                <el-icon><Tools /></el-icon>
                工具列表
              </span>
              <div>
                <el-tag size="small">启用: {{ toolData.enabled_tools || '全部' }}</el-tag>
                <el-button size="small" link @click="loadTools">
                  <el-icon><RefreshLeft /></el-icon>
                  刷新
                </el-button>
              </div>
            </div>
          </template>
          <el-tabs v-model="activeTab">
            <el-tab-pane :label="`内置/Toolkit (${toolData.builtin?.length || 0})`" name="builtin">
              <el-table :data="toolData.builtin" size="small" stripe>
                <el-table-column prop="name" label="工具名" min-width="160">
                  <template #default="{ row }">
                    <el-link type="primary" @click="selectToolForTest(row)">{{ row.name }}</el-link>
                  </template>
                </el-table-column>
                <el-table-column prop="category" label="类别" width="100">
                  <template #default="{ row }">
                    <el-tag size="small" :type="row.category === 'toolkit' ? 'warning' : 'info'">
                      {{ row.category }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="description" label="描述" min-width="240" show-overflow-tooltip />
                <el-table-column label="启用" width="80">
                  <template #default="{ row }">
                    <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
                      {{ row.enabled ? '启用' : '禁用' }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="操作" width="100">
                  <template #default="{ row }">
                    <el-button size="small" link @click="selectToolForTest(row)">测试</el-button>
                  </template>
                </el-table-column>
              </el-table>
            </el-tab-pane>
            <el-tab-pane :label="`MCP (${toolData.mcp?.length || 0})`" name="mcp">
              <el-table :data="toolData.mcp" size="small" stripe>
                <el-table-column prop="name" label="工具名" min-width="160">
                  <template #default="{ row }">
                    <el-link type="primary" @click="selectToolForTest(row)">{{ row.name }}</el-link>
                  </template>
                </el-table-column>
                <el-table-column prop="description" label="描述" min-width="280" show-overflow-tooltip />
                <el-table-column label="操作" width="100">
                  <template #default="{ row }">
                    <el-button size="small" link @click="selectToolForTest(row)">测试</el-button>
                  </template>
                </el-table-column>
              </el-table>
              <el-empty v-if="!toolData.mcp?.length" description="未配置 MCP 服务器或未提供工具" />
            </el-tab-pane>
            <el-tab-pane :label="`自定义工具 (${customTools.length})`" name="custom">
              <div class="card-header-row mb-16">
                <el-button size="small" type="primary" @click="openImportDialog">
                  <el-icon><Upload /></el-icon>
                  导入 OpenAPI
                </el-button>
                <el-button size="small" link @click="loadCustomTools">
                  <el-icon><RefreshLeft /></el-icon>
                  刷新
                </el-button>
              </div>
              <el-table :data="customTools" size="small" stripe v-loading="customLoading">
                <el-table-column prop="name" label="工具名" min-width="140">
                  <template #default="{ row }">
                    <el-link type="primary" @click="openCustomDetail(row)">{{ row.name }}</el-link>
                  </template>
                </el-table-column>
                <el-table-column prop="description" label="描述" min-width="200" show-overflow-tooltip />
                <el-table-column prop="base_url" label="Base URL" min-width="200" show-overflow-tooltip />
                <el-table-column prop="auth_type" label="鉴权" width="100">
                  <template #default="{ row }">
                    <el-tag size="small" :type="row.auth_type === 'none' ? 'info' : 'warning'">
                      {{ row.auth_type }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="状态" width="80">
                  <template #default="{ row }">
                    <el-tag :type="row.enabled ? 'success' : 'info'" size="small">
                      {{ row.enabled ? '启用' : '禁用' }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="操作" width="200">
                  <template #default="{ row }">
                    <el-button size="small" link @click="openTestDialog(row)">测试</el-button>
                    <el-button size="small" link @click="toggleCustomTool(row)">
                      {{ row.enabled ? '禁用' : '启用' }}
                    </el-button>
                    <el-button size="small" link type="danger" @click="deleteCustomTool(row)">删除</el-button>
                  </template>
                </el-table-column>
              </el-table>
              <el-empty v-if="!customTools.length" description="未导入自定义工具,点击「导入 OpenAPI」开始" />
            </el-tab-pane>
          </el-tabs>
        </el-card>
      </el-col>

      <!-- 工具测试面板 -->
      <el-col :span="10">
        <el-card class="test-panel">
          <template #header>
            <span class="section-title">
              <el-icon><Cpu /></el-icon>
              工具测试面板
            </span>
          </template>
          <el-form label-position="top">
            <el-form-item label="工具名">
              <el-input v-model="testForm.tool_name" placeholder="选择左侧工具或手动输入" clearable />
            </el-form-item>
            <el-form-item label="参数 JSON">
              <el-input
                v-model="testArgsText"
                type="textarea"
                :rows="6"
                placeholder="{&quot;employee_id&quot;: &quot;u001&quot;, &quot;period&quot;: &quot;2025-W01&quot;}"
              />
              <span class="field-hint">
                参考 schema 提供参数。calculator 用 {"expression": "1+2*3"}
              </span>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="testing" @click="runTest">
                <el-icon><VideoPlay /></el-icon>
                运行测试
              </el-button>
              <el-button @click="clearTest">清空</el-button>
            </el-form-item>
          </el-form>
          <div v-if="testResult" class="test-result">
            <div class="result-header">
              <span class="result-title">执行结果</span>
              <el-tag :type="testResult.success ? 'success' : 'danger'" size="small">
                {{ testResult.success ? '成功' : '失败' }}
              </el-tag>
            </div>
            <pre class="result-pre">{{ testResult.result || testResult.error || '(空)' }}</pre>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- MCP 服务器管理 -->
    <el-card class="mt-16">
      <template #header>
        <div class="card-header-row">
          <span class="section-title">
            <el-icon><Link /></el-icon>
            MCP 服务器管理
          </span>
          <div>
            <el-button size="small" type="success" @click="openAddServerDialog">
              <el-icon><Plus /></el-icon>
              添加服务器
            </el-button>
            <el-button size="small" type="primary" @click="refreshMcpTools">
              <el-icon><Refresh /></el-icon>
              刷新工具
            </el-button>
            <el-button size="small" @click="openMcpConfigDialog">
              <el-icon><Setting /></el-icon>
              更新配置
            </el-button>
            <el-button size="small" link @click="loadMcpServers">
              <el-icon><RefreshLeft /></el-icon>
              刷新
            </el-button>
          </div>
        </div>
      </template>
      <el-alert
        v-if="!mcpData.mcp_available"
        type="warning"
        :closable="false"
        show-icon
        class="mb-16"
      >
        MCP 适配器未安装。请安装 <code>langchain-mcp-adapters</code> 后使用 MCP 服务器。
      </el-alert>
      <el-table :data="mcpData.servers" stripe v-loading="mcpLoading">
        <el-table-column prop="name" label="服务器名" min-width="140" />
        <el-table-column prop="transport" label="传输协议" width="120">
          <template #default="{ row }">
            <el-tag size="small">{{ row.transport }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.connected ? 'success' : 'info'" size="small">
              {{ row.connected ? '已连接' : '未连接' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="tools_count" label="工具数" width="90" align="center" />
        <el-table-column prop="endpoint" label="端点/命令" min-width="220" show-overflow-tooltip />
        <el-table-column label="操作" width="160">
          <template #default="{ row }">
            <el-button
              size="small"
              type="primary"
              :loading="testingMcp === row.name"
              @click="testMcpConnection(row.name)"
            >
              测试连接
            </el-button>
            <el-button
              size="small"
              link
              type="danger"
              @click="deleteMcpServer(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- MCP 工具列表展示 (从各 MCP 服务器获取的工具) -->
      <div v-if="mcpTools.length" class="mcp-tools-section">
        <div class="raw-config-title">
          <el-icon><Box /></el-icon>
          MCP 工具列表 ({{ mcpTools.length }} 个)
        </div>
        <el-table :data="mcpTools" size="small" stripe max-height="280">
          <el-table-column prop="name" label="工具名" min-width="180">
            <template #default="{ row }">
              <el-link type="primary" @click="selectToolForTest(row)">{{ row.name }}</el-link>
            </template>
          </el-table-column>
          <el-table-column prop="server" label="来源服务器" width="140">
            <template #default="{ row }">
              <el-tag size="small" type="info">{{ row.server || '-' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="description" label="描述" min-width="280" show-overflow-tooltip />
          <el-table-column label="操作" width="80">
            <template #default="{ row }">
              <el-button size="small" link @click="selectToolForTest(row)">测试</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
      <div v-if="mcpData.raw_config" class="raw-config">
        <div class="raw-config-title">当前配置 (JSON)</div>
        <pre class="config-pre">{{ formatJson(mcpData.raw_config) }}</pre>
      </div>
    </el-card>

    <!-- ReAct Agent 调试 -->
    <el-card class="mt-16">
      <template #header>
        <span class="section-title">
          <el-icon><MagicStick /></el-icon>
          ReAct Agent 调试
        </span>
      </template>
      <el-alert type="info" :closable="false" show-icon class="mb-16">
        ReAct Agent 适用于复杂推理任务:LLM 自主选择工具、多轮推理、最终汇总答案。
        与固定评估流水线不同,这里是开放式的对话式调试。
      </el-alert>
      <el-form label-position="top">
        <el-form-item label="会话 ID (可选,多轮对话用)">
          <el-input v-model="reactForm.thread_id" placeholder="留空则单轮,填入则同 ID 复用上下文" />
        </el-form-item>
        <el-form-item label="用户消息">
          <el-input
            v-model="reactForm.message"
            type="textarea"
            :rows="4"
            placeholder="如:分析 u001 本周表现趋势并给出改进建议"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="invokingReact" @click="invokeReAct">
            <el-icon><VideoPlay /></el-icon>
            调用 ReAct Agent
          </el-button>
        </el-form-item>
      </el-form>
      <div v-if="reactResult" class="react-result">
        <div class="result-header">
          <span class="result-title">Agent 输出</span>
          <el-tag v-if="reactResult.iterations !== undefined" size="small">
            迭代 {{ reactResult.iterations }} 次
          </el-tag>
        </div>
        <pre class="result-pre">{{ reactResult.answer || reactResult.result || JSON.stringify(reactResult, null, 2) }}</pre>
      </div>
    </el-card>

    <!-- MCP 配置更新对话框 -->
    <el-dialog
      v-model="mcpConfigDialogVisible"
      title="更新 MCP 配置 (热更新)"
      width="720px"
    >
      <el-alert type="warning" :closable="false" show-icon class="mb-16">
        更新后立即生效,无需重启服务。配置格式为 JSON 对象,键为服务器名。
      </el-alert>
      <el-form label-position="top">
        <el-form-item label="MCP 服务器配置 (JSON)">
          <el-input
            v-model="mcpConfigForm.mcp_servers"
            type="textarea"
            :rows="10"
            placeholder="{&quot;filesystem&quot;: {&quot;transport&quot;: &quot;stdio&quot;, &quot;command&quot;: &quot;npx&quot;, &quot;args&quot;: [&quot;-y&quot;, &quot;@modelcontextprotocol/server-filesystem&quot;, &quot;/tmp&quot;]}, &quot;remote&quot;: {&quot;transport&quot;: &quot;streamable_http&quot;, &quot;url&quot;: &quot;http://localhost:8080/mcp&quot;}}"
          />
        </el-form-item>
        <el-form-item label="启用工具列表 (CSV)">
          <el-input
            v-model="mcpConfigForm.enabled_tools"
            placeholder="calculator,datetime,employee_history (留空表示全部启用)"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="mcpConfigDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="updatingMcp" @click="submitMcpConfig">更新</el-button>
      </template>
    </el-dialog>

    <!-- 添加 MCP 服务器对话框 (结构化表单) -->
    <el-dialog
      v-model="addServerDialogVisible"
      title="添加 MCP 服务器"
      width="640px"
      :close-on-click-modal="false"
    >
      <el-alert type="info" :closable="false" show-icon class="mb-16">
        填写服务器配置后,将合并到现有 MCP 配置并热更新生效。
        stdio 适合本地进程 (如 npx 启动),streamable_http / sse 适合远程服务。
      </el-alert>
      <el-form ref="addServerFormRef" :model="addServerForm" :rules="addServerRules" label-position="top">
        <el-form-item label="服务器名称 (唯一标识)" prop="server_name">
          <el-input
            v-model="addServerForm.server_name"
            placeholder="如 filesystem / github / my-remote-mcp"
            maxlength="60"
          />
        </el-form-item>
        <el-form-item label="传输方式 (Transport)" prop="transport">
          <el-radio-group v-model="addServerForm.transport">
            <el-radio-button label="stdio">stdio (本地进程)</el-radio-button>
            <el-radio-button label="streamable_http">streamable_http</el-radio-button>
            <el-radio-button label="sse">sse</el-radio-button>
          </el-radio-group>
        </el-form-item>

        <!-- stdio 模式: command + args -->
        <template v-if="addServerForm.transport === 'stdio'">
          <el-form-item label="命令 (command)" prop="command">
            <el-input
              v-model="addServerForm.command"
              placeholder="如 npx / node / python"
            />
          </el-form-item>
          <el-form-item label="参数 (args, 每行一个)">
            <el-input
              v-model="addServerForm.argsText"
              type="textarea"
              :rows="4"
              placeholder="-y&#10;@modelcontextprotocol/server-filesystem&#10;/tmp"
            />
            <span class="field-hint">每行一个参数,将转为 JSON 数组传给 MCP 适配器。</span>
          </el-form-item>
          <el-form-item label="环境变量 (JSON, 可选)">
            <el-input
              v-model="addServerForm.envText"
              type="textarea"
              :rows="3"
              placeholder='{"API_KEY": "xxx"}'
            />
          </el-form-item>
        </template>

        <!-- http / sse 模式: url + headers -->
        <template v-else>
          <el-form-item label="URL" prop="url">
            <el-input
              v-model="addServerForm.url"
              placeholder="https://example.com/mcp 或 http://localhost:8080/sse"
            />
          </el-form-item>
          <el-form-item label="请求头 (headers, JSON, 可选)">
            <el-input
              v-model="addServerForm.headersText"
              type="textarea"
              :rows="4"
              placeholder='{"Authorization": "Bearer xxx"}'
            />
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="addServerDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="addingServer" @click="saveNewServer">添加并热更新</el-button>
      </template>
    </el-dialog>

    <!-- 自定义工具: 导入 OpenAPI 对话框 -->
    <el-dialog
      v-model="importDialogVisible"
      title="导入 OpenAPI Schema (对标 Dify Custom Tool)"
      width="900px"
    >
      <el-alert type="info" :closable="false" show-icon class="mb-16">
        粘贴 OpenAPI 3.x JSON 或 YAML → 解析 paths → 每个 operation 生成一个 LangChain Tool。
        点击「解析预览」查看即将生成的工具列表,确认无误后保存。
      </el-alert>
      <el-form label-position="top">
        <el-form-item label="工具名 (租户内唯一)">
          <el-input v-model="importForm.name" placeholder="如 pet-store-api" />
        </el-form-item>
        <el-form-item label="工具描述">
          <el-input v-model="importForm.description" placeholder="可选,简短描述" />
        </el-form-item>
        <el-form-item label="Base URL">
          <el-input v-model="importForm.base_url" placeholder="https://api.example.com/v1" />
        </el-form-item>
        <el-form-item label="OpenAPI Schema (JSON 或 YAML)">
          <el-input
            v-model="importForm.raw"
            type="textarea"
            :rows="10"
            placeholder='{"openapi":"3.0.0","paths":{"/pets":{"get":{"operationId":"listPets","summary":"List all pets"}}}}'
          />
        </el-form-item>
        <el-form-item label="鉴权类型">
          <el-select v-model="importForm.auth_type" style="width: 200px">
            <el-option label="无 (none)" value="none" />
            <el-option label="Bearer Token" value="bearer" />
            <el-option label="API Key" value="api_key" />
            <el-option label="Basic Auth" value="basic" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="importForm.auth_type !== 'none'" label="凭证">
          <el-input
            v-model="importForm.auth_credentials"
            type="password"
            show-password
            placeholder="凭证将用 FieldCipher 加密存储"
          />
        </el-form-item>
      </el-form>
      <div v-if="parsePreview" class="parse-preview mb-16">
        <div class="result-header">
          <span class="result-title">解析预览 ({{ parsePreview.count }} 个工具)</span>
        </div>
        <el-table :data="parsePreview.tools" size="small" stripe max-height="300">
          <el-table-column prop="name" label="工具名" min-width="140" />
          <el-table-column prop="method" label="方法" width="80">
            <template #default="{ row }">
              <el-tag size="small" :type="methodTagType(row.method)">{{ row.method }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="path" label="Path" min-width="180" />
          <el-table-column prop="description" label="描述" min-width="200" show-overflow-tooltip />
        </el-table>
      </div>
      <template #footer>
        <el-button @click="importDialogVisible = false">取消</el-button>
        <el-button :loading="parsing" @click="parseOpenAPI">解析预览</el-button>
        <el-button type="primary" :loading="savingCustom" @click="saveCustomTool">保存</el-button>
      </template>
    </el-dialog>

    <!-- 自定义工具: 测试对话框 -->
    <el-dialog
      v-model="testDialogVisible"
      :title="`测试自定义工具 - ${testTarget?.name || ''}`"
      width="800px"
    >
      <el-alert type="info" :closable="false" show-icon class="mb-16">
        选择 path 与 method,填入参数,实际调用 HTTP endpoint 返回响应。
      </el-alert>
      <el-form label-position="top">
        <el-form-item label="Path">
          <el-select v-model="testTargetPath" placeholder="选择 path" style="width: 100%">
            <el-option
              v-for="t in testTargetTools"
              :key="t.path + '_' + t.method"
              :label="`${t.method} ${t.path}`"
              :value="t.path + '|' + t.method"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="参数 JSON">
          <el-input
            v-model="testParametersText"
            type="textarea"
            :rows="6"
            placeholder='{"limit": 5} 或 {"petId": "42"}'
          />
          <span class="field-hint">
            参考 path 参数 (如 {petId}) / query / header / body 合并提供
          </span>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="testingCustom" @click="runCustomTest">
            <el-icon><VideoPlay /></el-icon>
            执行测试
          </el-button>
        </el-form-item>
      </el-form>
      <div v-if="customTestResult" class="test-result">
        <div class="result-header">
          <span class="result-title">响应</span>
          <el-tag :type="customTestResult.success ? 'success' : 'danger'" size="small">
            {{ customTestResult.success ? '成功' : '失败' }}
          </el-tag>
        </div>
        <pre class="result-pre">{{ customTestResult.result || customTestResult.error || '(空)' }}</pre>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { toolAdminApi, customToolAdminApi } from '@/api/client'

// ====== 工具列表状态 ======
const loading = ref(false)
const activeTab = ref('builtin')
const toolData = ref({
  builtin: [],
  mcp: [],
  langchain_available: false,
  mcp_available: false,
  enabled_tools: null,
  mcp_servers: [],
})

async function loadTools() {
  loading.value = true
  try {
    const data = await toolAdminApi.listTools()
    toolData.value = data
  } catch (err) {
    ElMessage.error('加载工具列表失败: ' + err.message)
  } finally {
    loading.value = false
  }
}

// ====== 工具测试面板 ======
const testing = ref(false)
const testForm = reactive({
  tool_name: '',
})
const testArgsText = ref('{}')
const testResult = ref(null)

function selectToolForTest(tool) {
  testForm.tool_name = tool.name
  // 根据工具名预填参数
  const presets = {
    calculator: '{"expression": "1 + 2 * 3"}',
    datetime: '{}',
    employee_history: '{"employee_id": "u001", "limit": 5}',
    company_kb: '{"query": "评估标准", "top_k": 3}',
  }
  testArgsText.value = presets[tool.name] || '{}'
  testResult.value = null
}

async function runTest() {
  if (!testForm.tool_name.trim()) {
    ElMessage.warning('请填写工具名')
    return
  }
  let args = {}
  if (testArgsText.value.trim()) {
    try {
      args = JSON.parse(testArgsText.value)
    } catch {
      ElMessage.error('参数 JSON 格式错误')
      return
    }
  }
  testing.value = true
  testResult.value = null
  try {
    testResult.value = await toolAdminApi.testTool({
      tool_name: testForm.tool_name.trim(),
      args,
    })
  } catch (err) {
    testResult.value = { success: false, error: err.message }
  } finally {
    testing.value = false
  }
}

function clearTest() {
  testForm.tool_name = ''
  testArgsText.value = '{}'
  testResult.value = null
}

// ====== MCP 服务器管理 ======
const mcpLoading = ref(false)
const mcpData = ref({
  servers: [],
  mcp_available: false,
  raw_config: null,
})
const testingMcp = ref('')

async function loadMcpServers() {
  mcpLoading.value = true
  try {
    const data = await toolAdminApi.listMcpServers()
    mcpData.value = data
  } catch (err) {
    ElMessage.error('加载 MCP 服务器失败: ' + err.message)
  } finally {
    mcpLoading.value = false
  }
}

async function testMcpConnection(serverName) {
  testingMcp.value = serverName
  try {
    const result = await toolAdminApi.testMcp({ server_name: serverName })
    if (result.success || result.connected) {
      ElMessage.success(`服务器 ${serverName} 连接成功`)
    } else {
      ElMessage.warning(`服务器 ${serverName} 连接失败: ${result.error || '未知错误'}`)
    }
    await loadMcpServers()
  } catch (err) {
    ElMessage.error('测试失败: ' + err.message)
  } finally {
    testingMcp.value = ''
  }
}

// ====== MCP 配置更新 ======
const mcpConfigDialogVisible = ref(false)
const updatingMcp = ref(false)
const mcpConfigForm = reactive({
  mcp_servers: '',
  enabled_tools: '',
})

function openMcpConfigDialog() {
  mcpConfigForm.mcp_servers =
    typeof mcpData.value.raw_config === 'string'
      ? mcpData.value.raw_config
      : mcpData.value.raw_config
        ? JSON.stringify(JSON.parse(mcpData.value.raw_config), null, 2)
        : ''
  mcpConfigForm.enabled_tools = toolData.value.enabled_tools || ''
  mcpConfigDialogVisible.value = true
}

async function submitMcpConfig() {
  if (mcpConfigForm.mcp_servers.trim()) {
    try {
      JSON.parse(mcpConfigForm.mcp_servers)
    } catch {
      ElMessage.error('MCP 配置 JSON 格式错误')
      return
    }
  }
  updatingMcp.value = true
  try {
    await toolAdminApi.updateMcpConfig({
      mcp_servers: mcpConfigForm.mcp_servers || null,
      enabled_tools: mcpConfigForm.enabled_tools || null,
    })
    ElMessage.success('MCP 配置已热更新')
    mcpConfigDialogVisible.value = false
    await loadTools()
    await loadMcpServers()
  } catch (err) {
    ElMessage.error('更新失败: ' + err.message)
  } finally {
    updatingMcp.value = false
  }
}

// ====== MCP 工具列表 (从 MCP 服务器获取的工具,展示在 MCP 管理区) ======
const mcpTools = computed(() => {
  const tools = toolData.value.mcp || []
  // 工具对象若携带 server / source 字段则展示归属,否则显示 '-'
  return tools.map((t) => ({
    ...t,
    server: t.server || t.source || '-',
  }))
})

// ====== 添加 MCP 服务器 (结构化表单 → 合并到 MCP 配置热更新) ======
const addServerDialogVisible = ref(false)
const addingServer = ref(false)
const addServerFormRef = ref(null)
const addServerForm = reactive({
  server_name: '',
  transport: 'stdio',
  command: '',
  argsText: '',
  envText: '',
  url: '',
  headersText: '',
})

const addServerRules = {
  server_name: [{ required: true, message: '请输入服务器名称', trigger: 'blur' }],
  transport: [{ required: true, message: '请选择传输方式', trigger: 'change' }],
  command: [
    {
      validator: (rule, value, callback) => {
        if (addServerForm.transport === 'stdio' && !value) {
          callback(new Error('stdio 模式下请填写命令'))
        } else {
          callback()
        }
      },
      trigger: 'blur',
    },
  ],
  url: [
    {
      validator: (rule, value, callback) => {
        if (addServerForm.transport !== 'stdio' && !value) {
          callback(new Error('请填写 URL'))
        } else {
          callback()
        }
      },
      trigger: 'blur',
    },
  ],
}

function openAddServerDialog() {
  addServerForm.server_name = ''
  addServerForm.transport = 'stdio'
  addServerForm.command = ''
  addServerForm.argsText = ''
  addServerForm.envText = ''
  addServerForm.url = ''
  addServerForm.headersText = ''
  addServerDialogVisible.value = true
}

// 把结构化表单组装成单个 MCP server 配置项
function buildServerConfig() {
  const cfg = { transport: addServerForm.transport }
  if (addServerForm.transport === 'stdio') {
    cfg.command = addServerForm.command
    // args: 每行一个,去空行
    cfg.args = addServerForm.argsText
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean)
    if (addServerForm.envText.trim()) {
      try {
        cfg.env = JSON.parse(addServerForm.envText)
      } catch {
        ElMessage.error('环境变量 JSON 格式错误')
        return null
      }
    }
  } else {
    cfg.url = addServerForm.url
    if (addServerForm.headersText.trim()) {
      try {
        cfg.headers = JSON.parse(addServerForm.headersText)
      } catch {
        ElMessage.error('请求头 headers JSON 格式错误')
        return null
      }
    }
  }
  return cfg
}

// 读取当前 raw_config 为对象 (容错)
function parseCurrentMcpConfig() {
  const raw = mcpData.value.raw_config
  if (!raw) return {}
  if (typeof raw === 'object') return { ...raw }
  try {
    return JSON.parse(raw)
  } catch {
    return {}
  }
}

async function saveNewServer() {
  if (!addServerFormRef.value) return
  try {
    await addServerFormRef.value.validate()
  } catch {
    return
  }
  const serverCfg = buildServerConfig()
  if (!serverCfg) return
  const name = addServerForm.server_name.trim()
  // 合并到现有配置 (同名则覆盖)
  const currentConfig = parseCurrentMcpConfig()
  currentConfig[name] = serverCfg
  addingServer.value = true
  try {
    await toolAdminApi.updateMcpConfig({
      mcp_servers: JSON.stringify(currentConfig, null, 2),
      enabled_tools: toolData.value.enabled_tools || null,
    })
    ElMessage.success(`服务器「${name}」已添加并热更新`)
    addServerDialogVisible.value = false
    await loadTools()
    await loadMcpServers()
  } catch (err) {
    ElMessage.error('添加失败: ' + err.message)
  } finally {
    addingServer.value = false
  }
}

async function deleteMcpServer(row) {
  try {
    await ElMessageBox.confirm(
      `确认删除 MCP 服务器「${row.name}」?将热更新配置生效。`,
      '删除确认',
      { type: 'warning' },
    )
  } catch {
    return
  }
  const currentConfig = parseCurrentMcpConfig()
  if (!(row.name in currentConfig)) {
    ElMessage.warning(`未在配置中找到服务器「${row.name}」,可能由后端默认加载`)
    return
  }
  delete currentConfig[row.name]
  try {
    await toolAdminApi.updateMcpConfig({
      mcp_servers: JSON.stringify(currentConfig, null, 2),
      enabled_tools: toolData.value.enabled_tools || null,
    })
    ElMessage.success(`已删除服务器「${row.name}」`)
    await loadTools()
    await loadMcpServers()
  } catch (err) {
    ElMessage.error('删除失败: ' + err.message)
  }
}

async function refreshMcpTools() {
  // 重新加载工具列表 + MCP 服务器列表,等价于刷新 MCP 工具
  try {
    await Promise.all([loadTools(), loadMcpServers()])
    ElMessage.success('MCP 工具已刷新')
  } catch (err) {
    ElMessage.error('刷新失败: ' + err.message)
  }
}

// ====== ReAct Agent 调试 ======
const invokingReact = ref(false)
const reactForm = reactive({
  message: '',
  thread_id: '',
})
const reactResult = ref(null)

async function invokeReAct() {
  if (!reactForm.message.trim()) {
    ElMessage.warning('请输入用户消息')
    return
  }
  invokingReact.value = true
  reactResult.value = null
  try {
    reactResult.value = await toolAdminApi.invokeReAct({
      message: reactForm.message,
      thread_id: reactForm.thread_id || null,
    })
  } catch (err) {
    reactResult.value = { error: err.message }
    ElMessage.error('调用失败: ' + err.message)
  } finally {
    invokingReact.value = false
  }
}

// ====== 自定义工具 (P3-1: OpenAPI Schema 导入) ======
const customLoading = ref(false)
const customTools = ref([])
const importDialogVisible = ref(false)
const parsing = ref(false)
const savingCustom = ref(false)
const parsePreview = ref(null)
const importForm = reactive({
  name: '',
  description: '',
  base_url: '',
  raw: '',
  auth_type: 'none',
  auth_credentials: '',
})

async function loadCustomTools() {
  customLoading.value = true
  try {
    const data = await customToolAdminApi.list()
    customTools.value = data.items || []
  } catch (err) {
    ElMessage.error('加载自定义工具失败: ' + err.message)
  } finally {
    customLoading.value = false
  }
}

function openImportDialog() {
  importForm.name = ''
  importForm.description = ''
  importForm.base_url = ''
  importForm.raw = ''
  importForm.auth_type = 'none'
  importForm.auth_credentials = ''
  parsePreview.value = null
  importDialogVisible.value = true
}

async function parseOpenAPI() {
  if (!importForm.raw.trim()) {
    ElMessage.warning('请粘贴 OpenAPI Schema')
    return
  }
  if (!importForm.base_url.trim()) {
    ElMessage.warning('请填写 Base URL')
    return
  }
  parsing.value = true
  parsePreview.value = null
  try {
    const data = await customToolAdminApi.parse({
      raw: importForm.raw,
      base_url: importForm.base_url,
    })
    parsePreview.value = data
    ElMessage.success(`解析成功,生成 ${data.count} 个工具`)
  } catch (err) {
    ElMessage.error('解析失败: ' + err.message)
  } finally {
    parsing.value = false
  }
}

async function saveCustomTool() {
  if (!importForm.name.trim()) {
    ElMessage.warning('请填写工具名')
    return
  }
  if (!importForm.raw.trim()) {
    ElMessage.warning('请粘贴 OpenAPI Schema')
    return
  }
  // 自动 parse 一次得到 dict (供后端 openapi_schema 字段)
  let specObj
  try {
    specObj = JSON.parse(importForm.raw)
  } catch {
    ElMessage.error('OpenAPI Schema 不是合法 JSON,请先点「解析预览」检查')
    return
  }
  savingCustom.value = true
  try {
    await customToolAdminApi.create({
      name: importForm.name,
      description: importForm.description,
      openapi_schema: specObj,
      base_url: importForm.base_url,
      auth_type: importForm.auth_type,
      auth_credentials: importForm.auth_credentials || null,
    })
    ElMessage.success('自定义工具已保存')
    importDialogVisible.value = false
    await loadCustomTools()
  } catch (err) {
    ElMessage.error('保存失败: ' + err.message)
  } finally {
    savingCustom.value = false
  }
}

async function toggleCustomTool(row) {
  const next = !row.enabled
  try {
    await customToolAdminApi.toggle(row.id, next)
    ElMessage.success(`已${next ? '启用' : '禁用'} ${row.name}`)
    await loadCustomTools()
  } catch (err) {
    ElMessage.error('切换失败: ' + err.message)
  }
}

async function deleteCustomTool(row) {
  try {
    await ElMessageBox.confirm(
      `确认删除自定义工具 ${row.name}?此操作不可恢复`,
      '删除确认',
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await customToolAdminApi.delete(row.id)
    ElMessage.success(`已删除 ${row.name}`)
    await loadCustomTools()
  } catch (err) {
    ElMessage.error('删除失败: ' + err.message)
  }
}

async function openCustomDetail(row) {
  try {
    const data = await customToolAdminApi.get(row.id)
    // 把详情塞回列表对应行 (或仅弹出展示)
    await ElMessageBox.alert(
      JSON.stringify(data, null, 2),
      `自定义工具详情 - ${row.name}`,
      { confirmButtonText: '关闭' },
    )
  } catch (err) {
    ElMessage.error('获取详情失败: ' + err.message)
  }
}

// ====== 自定义工具测试对话框 ======
const testDialogVisible = ref(false)
const testingCustom = ref(false)
const testTarget = ref(null)
const testTargetTools = ref([])
const testTargetPath = ref('')
const testParametersText = ref('{}')
const customTestResult = ref(null)

async function openTestDialog(row) {
  testTarget.value = row
  customTestResult.value = null
  testTargetPath.value = ''
  testParametersText.value = '{}'
  // 拉取详情得到 tools 列表 (path + method)
  try {
    const data = await customToolAdminApi.get(row.id)
    testTargetTools.value = data.tools || []
  } catch (err) {
    ElMessage.error('获取工具列表失败: ' + err.message)
    testTargetTools.value = []
  }
  testDialogVisible.value = true
}

async function runCustomTest() {
  if (!testTarget.value) return
  if (!testTargetPath.value) {
    ElMessage.warning('请选择 path')
    return
  }
  const [path, method] = testTargetPath.value.split('|')
  let params = {}
  if (testParametersText.value.trim()) {
    try {
      params = JSON.parse(testParametersText.value)
    } catch {
      ElMessage.error('参数 JSON 格式错误')
      return
    }
  }
  testingCustom.value = true
  customTestResult.value = null
  try {
    customTestResult.value = await customToolAdminApi.test(testTarget.value.id, {
      path,
      method,
      parameters: params,
    })
  } catch (err) {
    customTestResult.value = { success: false, error: err.message }
  } finally {
    testingCustom.value = false
  }
}

function methodTagType(method) {
  const m = (method || '').toUpperCase()
  if (m === 'GET') return 'success'
  if (m === 'POST') return 'primary'
  if (m === 'PUT' || m === 'PATCH') return 'warning'
  if (m === 'DELETE') return 'danger'
  return 'info'
}

// ====== 工具函数 ======
function formatJson(str) {
  if (!str) return '(空)'
  try {
    return JSON.stringify(JSON.parse(str), null, 2)
  } catch {
    return str
  }
}

onMounted(() => {
  loadTools()
  loadMcpServers()
  loadCustomTools()
})
</script>

<style scoped>
.mb-16 {
  margin-bottom: 16px;
}
.mt-16 {
  margin-top: 16px;
}
.section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}
.card-header-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.stat-card {
  display: flex;
  align-items: center;
  gap: 12px;
}
.stat-icon {
  width: 48px;
  height: 48px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  color: #fff;
}
.stat-icon.langchain {
  background-color: #2563eb;
}
.stat-icon.mcp {
  background-color: #10b981;
}
.stat-icon.builtin {
  background-color: #f59e0b;
}
.stat-icon.mcp-tools {
  background-color: #8b5cf6;
}
.stat-body {
  flex: 1;
}
.stat-label {
  color: #909399;
  font-size: 12px;
  margin-bottom: 4px;
}
.stat-value {
  font-size: 18px;
  font-weight: 600;
}
.test-panel {
  position: sticky;
  top: 16px;
}
.field-hint {
  color: #909399;
  font-size: 12px;
  display: block;
  margin-top: 4px;
}
.test-result {
  margin-top: 16px;
}
.result-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.result-title {
  font-weight: 600;
  color: #303133;
}
.result-pre {
  background-color: #f5f7fa;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 12px;
  font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 400px;
  overflow: auto;
}
.raw-config {
  margin-top: 16px;
}
.raw-config-title {
  font-weight: 600;
  margin-bottom: 8px;
  color: #303133;
  display: flex;
  align-items: center;
  gap: 6px;
}
.mcp-tools-section {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px dashed var(--el-border-color-lighter);
}
.parse-preview {
  background-color: #fafafa;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 12px;
}
.config-pre {
  background-color: #fafafa;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 12px;
  font-family: ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 300px;
  overflow: auto;
}
.react-result {
  margin-top: 16px;
}
</style>
