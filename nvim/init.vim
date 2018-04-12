set number relativenumber
set encoding=utf-8
set wildmenu 

" disable backup
set nobackup
set nowb
set noswapfile

set tabstop=4
set shiftwidth=4
set softtabstop=4
set expandtab
set smarttab

set ai " auto indent
set si " smart indent
set wrap " wrap lines

" buftabline
set hidden
nnoremap <C-N> :bnext<CR> 
nnoremap <C-P> :bprev<CR> 

color material-monokai 

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

call plug#end()

autocmd vimenter * NERDTree
