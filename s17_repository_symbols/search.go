// 来自 s16：保持；仅将 JSON 请求转换成 Zoekt 查询；索引与匹配算法不在此重新实现。
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"regexp"
	"regexp/syntax"
	"time"
	"unicode/utf8"

	"github.com/sourcegraph/zoekt"
	"github.com/sourcegraph/zoekt/query"
	"github.com/sourcegraph/zoekt/search"
)

// s17 修改：mode 在同一索引中选择文本或符号查询。
type request struct {
	Mode  string `json:"mode"`
	Query string `json:"query"`
	Scope string `json:"scope"`
	Limit int    `json:"limit"`
}

type fileHit struct {
	Path  string `json:"path"`
	Lines []int  `json:"lines"`
}

type response struct {
	Files      []fileHit `json:"files"`
	Exhaustive bool      `json:"exhaustive"`
	Truncated  bool      `json:"truncated"`
}

// 来自 s16：保持；输入为有界字面文本；输出只含位置，不把大量源码从容器重复传回。
// 流程：校验请求 → 构造内容/目录条件 → Zoekt 排序搜索 → 按文件返回少量行号。
func run() error {
	var req request
	decoder := json.NewDecoder(io.LimitReader(os.Stdin, 8192))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&req); err != nil {
		return err
	}
	if req.Query == "" || utf8.RuneCountInString(req.Query) > 256 || req.Limit < 1 || req.Limit > 20 {
		return fmt.Errorf("查询或文件上限无效")
	}
	// s17 新增：Python 始终传入已选模式；非法值不能静默退化成文本搜索。
	if req.Mode != "text" && req.Mode != "symbol" {
		return fmt.Errorf("mode 必须是 text 或 symbol")
	}
	searcher, err := search.NewDirectorySearcher("/index")
	if err != nil {
		return err
	}
	defer searcher.Close()
	// 来自 s16：保持；不使用 query.Parse，file:、OR、正则字符都只是普通查询文本。
	var q query.Q = &query.Substring{Pattern: req.Query, Content: true, CaseSensitive: true}
	// s17 修改：参考 Sourcebot/Zoekt，由引擎限制为符号区间，不在 Python 全仓库扫描名称。
	if req.Mode == "symbol" {
		q = &query.Symbol{Expr: q}
	}
	if req.Scope != "" {
		pattern, err := syntax.Parse("^"+regexp.QuoteMeta(req.Scope)+"/", syntax.Perl)
		if err != nil {
			return err
		}
		q = query.NewAnd(q, &query.Regexp{Regexp: pattern, FileName: true, CaseSensitive: true})
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	// 来自 s16：保持；显示上限与扫描上限分开；不能把前十个结果声称为全部匹配。
	opts := &zoekt.SearchOptions{
		MaxDocDisplayCount: req.Limit + 1,
		ShardMaxMatchCount: 10000,
		TotalMaxMatchCount: 10000,
		MaxWallTime:        10 * time.Second,
	}
	result, err := searcher.Search(ctx, q, opts)
	if err != nil {
		return err
	}
	if result.Crashes > 0 {
		return fmt.Errorf("搜索分片失败")
	}
	out := response{
		Files:      []fileHit{},
		Exhaustive: result.FilesSkipped == 0 && result.ShardsSkipped == 0 && ctx.Err() == nil && result.MatchCount < 10000,
		Truncated:  result.FileCount > req.Limit,
	}
	for _, file := range result.Files {
		if len(out.Files) == req.Limit {
			out.Truncated = true
			break
		}
		hit := fileHit{Path: file.FileName, Lines: []int{}}
		// 来自 s16：保持；范围条件也可能产生文件名匹配，只收集真正的内容行。
		for _, line := range file.LineMatches {
			if line.FileName {
				continue
			}
			if len(hit.Lines) == 3 {
				out.Truncated = true
				break
			}
			hit.Lines = append(hit.Lines, line.LineNumber)
		}
		out.Files = append(out.Files, hit)
	}
	return json.NewEncoder(os.Stdout).Encode(out)
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
