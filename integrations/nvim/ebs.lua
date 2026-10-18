-- EBS for Neovim 0.11+: flow.yaml schema for yaml-language-server, and :Ebs* commands.
-- Copy to ~/.config/nvim/plugin/ebs.lua (needs yaml-language-server on PATH).

vim.lsp.config('yamlls', {
  settings = {
    yaml = {
      schemas = {
        ['https://mouettesylvain-spec.github.io/ebs/schemas/flow.v1.schema.json'] = {
          'flow.yaml',
          'flow.*.yaml',
        },
      },
    },
  },
})
vim.lsp.enable('yamlls')

-- Run a command in a terminal split, from the directory of the current file.
local function run(cmd)
  local dir = vim.fn.expand('%:p:h')
  vim.cmd('botright split')
  vim.fn.jobstart(cmd, { cwd = dir, term = true })
end

vim.api.nvim_create_user_command('EbsPlan', function()
  run('ebs plan')
end, {})
vim.api.nvim_create_user_command('EbsBuild', function(opts)
  run('ebs build -k ' .. opts.args)
end, { nargs = '*' })
vim.api.nvim_create_user_command('EbsStatus', function()
  run('ebs status')
end, {})
vim.api.nvim_create_user_command('EbsLogs', function(opts)
  run("ebs logs '" .. opts.args .. "'")
end, { nargs = 1 })

-- :EbsCheck puts flow errors (`ebs: error: file:line:col: message`) in the quickfix list.
vim.api.nvim_create_user_command('EbsCheck', function()
  local out = vim.fn.systemlist({ 'ebs', 'plan', '-f', vim.fn.expand('%:p') })
  vim.fn.setqflist({}, 'r', { lines = out, efm = 'ebs: error: %f:%l:%c: %m' })
  vim.cmd('cwindow')
end, {})
