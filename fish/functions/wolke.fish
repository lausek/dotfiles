function wolke 
    for f in $argv 
        set json (curl -i -#S -F "files[]=@$f" "https://wolke.onee.moe/upload?output=Text")
        set res (echo "$json" | sed 's/[\{\[,]\s/\n/g' | awk '$1 == "\"url\":"{print $2}')
        echo (string replace -ar '"' '' $res)
    end
end
