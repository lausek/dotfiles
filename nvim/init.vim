set nocompatible

call plug#begin('~/.config/nvim/plugins/')

Plug 'scrooloose/nerdtree'
Plug 'xuyuanp/nerdtree-git-plugin'
Plug 'roxma/nvim-completion-manager'
Plug 'valloric/youcompleteme'
Plug 'tpope/vim-surround'
Plug 'itchyny/lightline.vim'
Plug 'ap/vim-buftabline'

" rust
Plug 'rust-lang/rust.vim'
Plug 'racer-rust/vim-racer'
Plug 'roxma/nvim-cm-racer'
Plug 'racer-rust/racer'

call plug#end()

filetype on
filetype off

filetype plugin indent on

syntax on
color material-monokai

set encoding=utf-8 nobomb
set spelllang=en_us

set number relativenumber

" --- performance ---
set hidden " buftabline
set ttyfast
set lazyredraw

" --- disable backup ---
set nobackup
set nowb
set noswapfile

" --- indentation ---
set ai " auto indent
set si " smart indent
set wrap " wrap lines

set tabstop=4
set shiftwidth=4
set softtabstop=4
set expandtab
set smarttab

" --- regex ---
set gdefault
set hlsearch
set incsearch
set ignorecase smartcase

" --- completion ---
set wildmenu
set wildchar=<TAB>
set wildmode=list:longest

" --- buffer navigation ---
nnoremap <C-N> :bnext<CR>
nnoremap <C-P> :bprev<CR>

" --- custom functions ---
function! StripWhitespace()
    let save_cursor = getpos(".")
    let old_query = getreg('/')
    :%s/\s\+$//e
    call setpos('.', save_cursor)
    call setreg('/', old_query)
endfunction

" remove last whitespace in whole file
noremap <leader>ss :call StripWhitespace()<CR>

" format whole code
noremap <leader>f gg=G

" avoid mistyping commands
command! W w
command! Wq wq
command! Wqa wqa

" --- plugin config ---
let NERDTreeMinimalUI=1
autocmd vimenter * NERDTree
