syntax on
set number
set relativenumber
set cursorline
set showcmd
set wildmenu
set hlsearch
set incsearch
exec "nohlsearch"


map s :w<CR>
map Q :q<CR>
let mapleader = " "
nnoremap <leader>n :set number! relativenumber!<CR>
" 搜索
set ignorecase       " 搜索忽略大小写
set smartcase        " 有大写字母时区分大小写
set noswapfile       " 不生成swap文件
set scrolloff=5      " 光标距顶/底保留5行
set mouse=
function! Osc52Yank()
  let buffer = system('base64 -w 0', @0)
  let buffer = "\033]52;c;" . buffer . "\007"
  silent exe "!echo -ne " . shellescape(buffer) . " > /dev/tty"
endfunction

autocmd TextYankPost * call Osc52Yank()
