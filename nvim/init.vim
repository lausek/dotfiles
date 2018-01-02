set number relativenumber  

color material-monokai 

set tabstop=4
set shiftwidth=4
set softtabstop=4
set expandtab

call plug#begin('~/.config/nvim/plugins/')

Plug 'scrooloose/nerdtree'

Plug 'xuyuanp/nerdtree-git-plugin'

Plug 'roxma/nvim-completion-manager'

" rust
Plug 'rust-lang/rust.vim'
Plug 'racer-rust/vim-racer'
Plug 'roxma/nvim-cm-racer'

call plug#end()

autocmd vimenter * NERDTree

